from api_models import Position,LongPosition, ShortPosition, OptionPosition
from api_models import Portfolio,StockPortfolio,ShortPortfolio,OptionPortfolio,OpenOrder
from api_models import StockQuote
from constants import OPTIONS_QUOTE_URL
from options import OptionChainLookup, OptionChain, OptionContract
from session_singleton import Session
from utils import UrlHelper, coerce_value
from lxml import html
import json
import re
from warnings import warn
import requests
import datetime
from ratelimit import limits,sleep_and_retry
from decimal import Decimal
import logging

@sleep_and_retry
@limits(calls=6,period=20)
def option_lookup(symbol,strike_price_proximity=3):
    logging.debug("OPTION LOOKUP FOR %s" % symbol)
    def filter_contracts(olist,stock_price,spp):
        if olist is None:
            return []
        middle_index = 0
        for i in range(len(olist)):
            if stock_price < olist[i]['StrikePrice']:
                middle_index += 1
                break
        start = middle_index - spp
        end = middle_index + spp
        if start < 0:
            start = 0
        if end > len(olist) - 1:
            end = len(olist) - 1

        return olist[start:end]


    session = Session()
    resp = session.get(UrlHelper.route('optionlookup'))
    tree = html.fromstring(resp.text)

    option_token = None
    option_user_id = None

    token = None
    user_id = None
    param_script = tree.xpath('//script[contains(text(),"quoteOptions")]/text()')[0]
    param_search = re.search(r'\#get\-quote\-options\'\)\,\s*\'(.+)\'\s*\,\s*(\d+)\s*\)\;', param_script)
    try:
        option_token = param_search.group(1)
        option_user_id = param_search.group(2)
    except Exception:
        raise Exception("Unable to get option lookup token")

    option_quote_qp = {
        'IdentifierType': 'Symbol',
        'Identifier': symbol,
        'SymbologyType': 'DTNSymbol',
        'OptionExchange': None,
        '_token': option_token,
        '_token_userid': option_user_id
    }

    url = UrlHelper.set_query(OPTIONS_QUOTE_URL, option_quote_qp)

    resp = requests.get(url)
    option_data = json.loads(resp.text)

    quote = option_data['Quote']
    if quote is None:
        logging.debug(option_data)
        raise Exception("No option quote data for %s" % symbol)


    try:
        last_price = option_data['Quote']['Last']
    except Exception as e:
        logging.debug(option_data)
        logging.debug(e)
    option_chains = []
    for e in option_data['Expirations']:
        expiration = e['ExpirationDate']
        filtered_calls = filter_contracts(e['Calls'],last_price,strike_price_proximity)
        filtered_puts = filter_contracts(e['Puts'],last_price,strike_price_proximity)
        

        calls = [OptionContract(o) for o in filtered_calls]
        puts = [OptionContract(o) for o in filtered_puts]
        option_chains.append(OptionChain(expiration,calls=calls,puts=puts))

        
    option_chain_lookup = OptionChainLookup(symbol,*option_chains)
    return option_chain_lookup

@sleep_and_retry
@limits(calls=6,period=20)
def stock_quote(symbol):
    url = UrlHelper.route('lookup')
    session = Session()
    resp = session.post(url, data={'symbol': symbol})
    resp.raise_for_status()
    try:
        tree = html.fromstring(resp.text)
    except Exception:
        warn("unable to get quote for %s" % symbol)
        return

    xpath_map = {
        'name': '//h3[@class="companyname"]/text()',
        'symbol': '//table[contains(@class,"table3")]/tbody/tr[1]/td[1]/h3[contains(@class,"pill")]/text()',
        'exchange': '//table[contains(@class,"table3")]//div[@class="marketname"]/text()',
        'last': '//table[@id="Table2"]/tbody/tr[1]/th[contains(text(),"Last")]/following-sibling::td/text()',
        'change': '//table[@id="Table2"]/tbody/tr[2]/th[contains(text(),"Change")]/following-sibling::td/text()',
        'change_percent': '//table[@id="Table2"]/tbody/tr[3]/th[contains(text(),"% Change")]/following-sibling::td/text()',
        'volume': '//table[@id="Table2"]/tbody/tr[4]/th[contains(text(),"Volume")]/following-sibling::td/text()',
        'days_high': '//table[@id="Table2"]/tbody/tr[5]/th[contains(text(),"Day\'s High")]/following-sibling::td/text()',
        'days_low': '//table[@id="Table2"]/tbody/tr[6]/th[contains(text(),"Day\'s Low")]/following-sibling::td/text()'
    }

    stock_quote_data = {}
    try:
        stock_quote_data = {
            k: str(tree.xpath(v)[0]).strip() for k, v in xpath_map.items()}
    except IndexError:
        warn("Unable to parse quote ")
        return
        

    exchange_matches = re.search(
        r'^\(([^\)]+)\)$', stock_quote_data['exchange'])
    if exchange_matches:
        stock_quote_data['exchange'] = exchange_matches.group(1)

    quote = StockQuote(**stock_quote_data)
    return quote

class QuoteWrapper(object):
    def __init__(self,symbol):
        self.symbol = symbol

    def wrap_quote(self):
        return stock_quote(self.symbol)

class OptionLookupWrapper(object):
    def __init__(self,underlying,contract_symbol,contract):
        self.underlying = underlying
        self.contract_symbol = contract_symbol
        self.contract = contract

    def wrap_quote(self):
        # check if contract is expired here before doing a lookup
        if datetime.date.today() > self.contract.expiration:
            return self.contract
        return option_lookup(self.underlying)[self.contract_symbol]

class CancelOrderWrapper(object):
    def __init__(self,link):
        self.link = link
    @sleep_and_retry
    @limits(calls=3,period=20)
    def wrap_cancel(self):
        url = "%s%s" % (UrlHelper.route('opentrades'),self.link)
        print(url)
        session = Session()
        session.get(url)
        

class Parsers(object):
    @staticmethod
    @sleep_and_retry
    @limits(calls=6,period=20)
    def get_open_trades(portfolio_tree):
        session = Session()
        open_trades_resp = session.get(UrlHelper.route('opentrades'))
        open_tree = html.fromstring(open_trades_resp.text)
        open_trade_rows = open_tree.xpath('//*[@id="Content"]/div[2]/div[2]/table/tbody/tr')[1:]

        ot_xpath_map = {
            'order_id': 'td[1]/text()',
            'symbol': 'td[5]/a/text()',
            'cancel_fn': 'td[2]/a/@href',
            'order_date': 'td[3]/text()',
            'quantity': 'td[6]/text()',
            'order_price': 'td[7]/text()',
            'trade_type' : 'td[4]/text()'
        
        }

        open_orders = []

        for tr in open_trade_rows:
            fon = lambda x: x[0] if len(x)> 0 else None
            open_order_dict = {k:fon(tr.xpath(v)) for k,v in ot_xpath_map.items()}
            symbol_match = re.search(r'^([^\.\d]+)',open_order_dict['symbol'])
            if symbol_match:
                open_order_dict['symbol'] = symbol_match.group(1)
            if open_order_dict['order_price'] == 'n/a':
                oid = open_order_dict['order_id']
                quantity = int(open_order_dict['quantity'])
                pxpath = '//table[@id="stock-portfolio-table"]//tr[contains(@style,"italic")]//span[contains(@id,"%s")]/ancestor::tr/td[5]/span/text()' % oid
                cancel_link = open_order_dict['cancel_fn']
                wrapper = CancelOrderWrapper(cancel_link)
                open_order_dict['cancel_fn'] = wrapper.wrap_cancel
                try:
                    current_price = coerce_value(fon(portfolio_tree.xpath(pxpath)),Decimal)
                    open_order_dict['order_price'] = current_price * quantity
                except Exception as e:
                    warn("Unable to parse open trade value for %s" % open_order_dict['symbol'])
                    open_order_dict['order_price'] = 0
                
                open_orders.append(OpenOrder(**open_order_dict))
        return open_orders


    @staticmethod
    @sleep_and_retry
    @limits(calls=6,period=20)
    def get_portfolio():
        session = Session()
        portfolio_response = session.get(UrlHelper.route('portfolio'))
        portfolio_tree = html.fromstring(portfolio_response.text)

        stock_portfolio = StockPortfolio()
        short_portfolio = ShortPortfolio()
        option_portfolio = OptionPortfolio()

        Parsers.parse_and_sort_positions(portfolio_tree,stock_portfolio,short_portfolio,option_portfolio)

        xpath_prefix = '//div[@id="infobar-container"]/div[@class="infobar-title"]/p'

        xpath_map = {
            'account_value': '/strong[contains(text(),"Account Value")]/following-sibling::span/text()',
            'buying_power':  '/strong[contains(text(),"Buying Power")]/following-sibling::span/text()',
            'cash':          '/strong[contains(text(),"Cash")]/following-sibling::span/text()',
            'annual_return_pct': '/strong[contains(text(),"Annual Return")]/following-sibling::span/text()',
        }

        xpath_get = lambda xpth: portfolio_tree.xpath("%s%s" % (xpath_prefix,xpth))[0]

        portfolio_args = {k: xpath_get(v)  for k,v in xpath_map.items()}
        portfolio_args['stock_portfolio'] = stock_portfolio
        portfolio_args['short_portfolio'] = short_portfolio
        portfolio_args['option_portfolio'] = option_portfolio
        portfolio_args['open_orders'] = Parsers.get_open_trades(portfolio_tree)
        for order in portfolio_args['open_orders']:
            print(order.__dict__)
        return Portfolio(**portfolio_args)

    @staticmethod
    def parse_and_sort_positions(tree,stock_portfolio,short_portfolio, option_portfolio):        
        trs = tree.xpath('//table[contains(@class,"table1")]/tbody/tr[not(contains(@class,"expandable")) and not(contains(@class,"no-border"))]')
        xpath_map = {
            'portfolio_id': 'td[1]/div/@data-portfolioid',
            # stock_type': 'td[1]/div/@data-stocktype',
            'symbol': 'td[1]/div/@data-symbol',
            'description': 'td[4]/text()',
            'quantity': 'td[5]/text()',
            'purchase_price': 'td[6]/text()',
            'current_price': 'td[7]/text()',
            'total_value': 'td[8]/text()',
        }

        for tr in trs:
            # <div class="detailButton btn-expand close" id="PS_LONG_0" data-symbol="TMO" data-portfolioid="5700657" data-stocktype="long"></div>
            fon = lambda x: x[0] if len(x)> 0 else None
            position_data = {k: fon(tr.xpath(v)) for k, v in xpath_map.items()}

            stock_type = fon(tr.xpath('td[1]/div/@data-stocktype'))
            trade_link = fon(tr.xpath('td[2]/a[2]/@href'))

            if stock_type is None or trade_link is None:
                continue

            elif stock_type == 'long':
                qw = QuoteWrapper(position_data['symbol']).wrap_quote
                long_pos = LongPosition(qw, stock_type, **position_data)
                stock_portfolio.append(long_pos)
            elif stock_type == 'short':
                qw = QuoteWrapper(position_data['symbol']).wrap_quote
                short_pos = ShortPosition(qw,stock_type, **position_data)
                short_portfolio.append(short_pos)
            elif stock_type == 'option':
                contract_symbol = position_data['symbol']
                oc = OptionContract(contract_name=position_data['symbol'])
                underlying = oc.base_symbol
                quote_fn = OptionLookupWrapper(underlying,contract_symbol,oc).wrap_quote
                option_pos = OptionPosition(oc,quote_fn,stock_type, **position_data)

                option_portfolio.append(option_pos)
