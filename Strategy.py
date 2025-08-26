import logging
import pyotp
import time
import datetime
import requests
import zipfile
import pandas as pd
from NorenRestApiPy.NorenApi import NorenApi
import Cred as keys
import Constants as const

CE_STRIKES = []
PE_STRIKES = []

CE_SYMBOL_LIST = []
PE_SYMBOL_LIST = []

CE_TOKEN_LIST = []
PE_TOKEN_LIST = []

CE_WEBSOCKET = {}
PE_WEBSOCKET = {}

ce_token = None
pe_token = None

first_ce_strike = None
first_pe_strike = None

ce_sl_order_no = None
pe_sl_order_no = None

feed_opened = False
universal_exit_triggered = False
first_order_placed = False

ce_first_sl_hit = False
pe_first_sl_hit = False
second_sl_hit = False

sl_not_placed_correctly = False
second_iteration_executed = False


def download_file(url, filename):
    response = requests.get(url)
    if response.status_code == 200:
        with open(filename, 'wb') as f:
            f.write(response.content)
    else:
        raise Exception(f"Failed to download file from {url}")


def token_lookup(instrument_df, ce_symbol_list, pe_symbol_list):
    global CE_TOKEN_LIST, PE_TOKEN_LIST

    # Lookup tokens for CE symbols
    for symbol in ce_symbol_list:
        token_series = instrument_df[instrument_df['TradingSymbol']
                                     == symbol]['Token']
        if not token_series.empty:
            token = int(token_series.iloc[0])
            CE_TOKEN_LIST.append(token)  # Add only the token
        else:
            print(f"Token not found for CE symbol: {symbol}")

    # Lookup tokens for PE symbols
    for symbol in pe_symbol_list:
        token_series = instrument_df[instrument_df['TradingSymbol']
                                     == symbol]['Token']
        if not token_series.empty:
            token = int(token_series.iloc[0])
            PE_TOKEN_LIST.append(token)  # Add only the token
        else:
            print(f"Token not found for PE symbol: {symbol}")

    return CE_TOKEN_LIST, PE_TOKEN_LIST


def get_atm_nifty(price):
    return round(float(price) / 50) * 50


def get_symbol(strike, option_type):
    if option_type not in ['C', 'P']:
        raise ValueError("Invalid option type. Must be 'C' or 'P'.")

    return f'NIFTY{const.EXPIRY_DATE}{const.EXPRITY_MONTH}25{option_type}{strike}'


def get_ltp(exchange, index):
    try:
        script = api.searchscrip(exchange=exchange, searchtext=index)
        token = script['values'][0]['token']
        return api.get_quotes(exchange=exchange, token=token)
    except IndexError:
        print(
            f"No results found for index '{index}' in exchange '{exchange}'.")
    except KeyError as e:
        print(f"KeyError: Missing expected key in API response: {e}")
    except Exception as e:
        print(f"An unexpected error occurred: {e}")


def generate_and_get_strikes(original_strike):
    global CE_STRIKES, PE_STRIKES, CE_SYMBOL_LIST, PE_SYMBOL_LIST

    # Adjusted strike values for CE and PE
    adjusted_ce = original_strike - 300
    adjusted_pe = original_strike + 300

    # Generate strike prices for CE and PE
    CE_STRIKES = [adjusted_ce + i for i in range(0, 1501, 100)]
    PE_STRIKES = [adjusted_pe - i for i in range(0, 1501, 100)]

    print('log 3 - strikes', CE_STRIKES, PE_STRIKES)

    # Generate symbols for CE and PE
    CE_SYMBOL_LIST = [
        f'NIFTY{const.EXPIRY_DATE}{const.EXPRITY_MONTH}25C{ce_strike}'
        for ce_strike in CE_STRIKES
    ]
    PE_SYMBOL_LIST = [
        f'NIFTY{const.EXPIRY_DATE}{const.EXPRITY_MONTH}25P{pe_strike}'
        for pe_strike in PE_STRIKES
    ]

    return CE_SYMBOL_LIST, PE_SYMBOL_LIST


def get_lp_value(token):
    print('Getting LP value for token:', token)
    global CE_WEBSOCKET, PE_WEBSOCKET

    try:
        # Ensure token is the correct type
        token = int(token)

        if token in CE_WEBSOCKET:
            print(f"Token {token} found in CE_WEBSOCKET")
            return CE_WEBSOCKET[token].get('lp')
        elif token in PE_WEBSOCKET:
            print(f"Token {token} found in PE_WEBSOCKET")
            return PE_WEBSOCKET[token].get('lp')
        else:
            print(f"Token {token} not found in either websocket")
            return None
    except ValueError:
        print(f"Invalid token format: {token}")
        return None
    except Exception as e:
        print(f"Error occurred: {e}")
        return None


def rounded_to_tick(value):
    return round(round(value / 0.05) * 0.05, 2)

# order placement


def universel_exit():
    while True:
        try:
            # Fetch positions and order book
            positions = pd.DataFrame(api.get_positions())
            orders = pd.DataFrame(api.get_order_book())
            break
        except Exception as e:
            print(f"uni_exit: Error fetching positions/orders: {e}")
            time.sleep(1)
            continue

    # Cancel all open or trigger-pending orders
    for order in orders.itertuples():
        try:
            if order.status in ['TRIGGER_PENDING', 'OPEN']:
                api.cancel_order(order.norenordno)
                print(f"uni_exit: Canceled order {order.norenordno}")
        except Exception as e:
            print(f"uni_exit: Error cancelling order {order.norenordno}: {e}")

    # Exit all open positions
    for position in positions.itertuples():
        try:
            net_qty = int(position.netqty)
            if net_qty < 0:  # Short position, buy to exit
                api.place_order(
                    buy_or_sell='B',
                    product_type=position.prd,
                    exchange=position.exch,
                    tradingsymbol=position.tsym,
                    quantity=abs(net_qty),
                    discloseqty=0,
                    price_type='MKT',
                    price=0,
                    trigger_price=None,
                    retention='DAY',
                    remarks='killswitch_buy'
                )
                print(f"uni_exit: Closed short position on {position.tsym}")
            elif net_qty > 0:  # Long position, sell to exit
                api.place_order(
                    buy_or_sell='S',
                    product_type=position.prd,
                    exchange=position.exch,
                    tradingsymbol=position.tsym,
                    quantity=net_qty,
                    discloseqty=0,
                    price_type='MKT',
                    price=0,
                    trigger_price=None,
                    retention='DAY',
                    remarks='killswitch_sell'
                )
                print(f"uni_exit: Closed long position on {position.tsym}")
        except Exception as e:
            print(f"uni_exit: Error closing position on {position.tsym}: {e}")


def place_order(buy_sell, symbol, quantity, product_type, exchange,
                price_type, price, trigger_price, remarks):
    try:
        return api.place_order(
            buy_or_sell=buy_sell,
            product_type=product_type,
            exchange=exchange,
            tradingsymbol=symbol,
            quantity=quantity,
            discloseqty=0,
            price_type=price_type,
            price=price,
            trigger_price=trigger_price,
            retention='DAY',
            remarks=remarks
        )
    except Exception as e:
        return f"An error occurred while placing the order: {e}"


def place_straddle_and_sl():
    global universal_exit_triggered, ce_token, pe_token, ce_sl_order_no, pe_sl_order_no, first_ce_strike, first_pe_strike

    try:
        # Fetch LTP and calculate ATM strike
        print('---------------------------')
        print('inside place straddle')
        nifty_ltp = get_ltp(const.EXCHANGE_NSE, const.NIFTY)
        atm_strike = get_atm_nifty(nifty_ltp['lp'])
        print(f"ATM Strike: {atm_strike}")

        # Generate CE and PE symbols
        ce_strike = get_symbol(atm_strike + 100, 'C')
        pe_strike = get_symbol(atm_strike - 100, 'P')

        first_ce_strike = ce_strike
        first_pe_strike = pe_strike

        print(f"CE Strike: {ce_strike}")
        print(f"PE Strike: {pe_strike}")

        # Place CE and PE sell orders
        place_ce = place_order(
            buy_sell=const.SELL,
            quantity=const.QUANTITY,
            product_type=const.PRODUCT_TYPE_MIS,
            exchange=const.EXCHANGE_NFO,
            symbol=ce_strike,
            price_type=const.PRICE_TYPE_MARKET,
            trigger_price=None,
            price=0,
            remarks='ce_sell'
        )
        place_pe = place_order(
            buy_sell=const.SELL,
            quantity=const.QUANTITY,
            product_type=const.PRODUCT_TYPE_MIS,
            exchange=const.EXCHANGE_NFO,
            symbol=pe_strike,
            price_type=const.PRICE_TYPE_MARKET,
            trigger_price=None,
            price=0,
            remarks='pe_sell'
        )

        # Validate order placement
        if not place_ce or place_ce.get('stat') != 'Ok' or not place_pe or place_pe.get('stat') != 'Ok':
            print("Error: One or both orders failed to place. Triggering universal exit.")
            universal_exit_triggered = True
            universel_exit()
            return

        time.sleep(3)

        # Retrieve average prices
        ce_order_status = api.single_order_history(
            orderno=place_ce['norenordno'])

        print(ce_order_status, 'ce order status')
        pe_order_status = api.single_order_history(
            orderno=place_pe['norenordno'])

        print(pe_order_status, 'pe order status')

        ce_token = ce_order_status[0]['token']
        pe_token = pe_order_status[0]['token']

        print('ce_token indsie place straddle', ce_token)
        print('pe_token indsie place straddle', pe_token)

        ce_avg_price = float(ce_order_status[0]['avgprc'])
        pe_avg_price = float(pe_order_status[0]['avgprc'])

        print('ce average price', ce_avg_price)
        print('pe average price', pe_avg_price)

        if ce_order_status[0]['status'] != 'COMPLETE' or pe_order_status[0]['status'] != 'COMPLETE':
            print(
                "Error: One or both orders are not completed. Triggering universal exit.")
            universal_exit_triggered = True
            universel_exit()
            return

        # Calculate SL trigger price and price (ensure multiple of 0.05)

        ce_trigger_price = rounded_to_tick(ce_avg_price * 1.3)
        ce_price = rounded_to_tick(ce_avg_price * 1.45)

        pe_trigger_price = rounded_to_tick(pe_avg_price * 1.3)
        pe_price = rounded_to_tick(pe_avg_price * 1.45)

        # Place SL orders for CE and PE
        ce_sl_order = place_order(
            buy_sell=const.BUY,
            quantity=const.QUANTITY,
            product_type=const.PRODUCT_TYPE_MIS,
            exchange=const.EXCHANGE_NFO,
            symbol=ce_strike,
            price_type=const.PRICE_TYPE_SL_LMT,
            price=ce_price,
            trigger_price=ce_trigger_price,
            remarks='ce_first_sl'
        )
        pe_sl_order = place_order(
            buy_sell=const.BUY,
            quantity=const.QUANTITY,
            product_type=const.PRODUCT_TYPE_MIS,
            exchange=const.EXCHANGE_NFO,
            symbol=pe_strike,
            price_type=const.PRICE_TYPE_SL_LMT,
            price=pe_price,
            trigger_price=pe_trigger_price,
            remarks='pe_first_sl'
        )


        print('ce Sl order', ce_sl_order)
        print('pe Sl order', pe_sl_order)

        if not ce_sl_order or ce_sl_order['stat'] != 'Ok' or not pe_sl_order or pe_sl_order['stat'] != 'Ok':
            print("Error: One or both SL orders failed. Triggering universal exit.")
            universal_exit_triggered = True
            universel_exit()
            return

        ce_sl_order_no = ce_sl_order['norenordno']
        pe_sl_order_no = pe_sl_order['norenordno']

        # Print SL order statuses
        print(f"CE SL Order: {ce_sl_order}")
        print(f"PE SL Order: {pe_sl_order}")

    except Exception as e:
        print(f"Error in place_straddle_and_sl: {e}")
        universal_exit_triggered = True
        universel_exit()

    finally:
        if universal_exit_triggered:
            print("Universal exit triggered due to an issue with order placement.")


def place_order_for_ce(pe_token):
    global universal_exit_triggered, pe_sl_order_no, first_pe_strike
    try:
        # Fetch the LTP value for PE
        print('pe token', pe_token)
        pe_ltp = get_lp_value(pe_token)
        print('log 4 - pe ltp:', pe_ltp)
        if pe_ltp is None:
            raise ValueError("Failed to fetch LTP for PE token.")

        # Find the closest CE token based on LTP comparison
        closest_token_ce = min(
            CE_WEBSOCKET,
            key=lambda token: abs(
                float(CE_WEBSOCKET[token].get('lp', 0)) - float(pe_ltp)),
            default=None
        )

        print('log 5 - token ce', closest_token_ce)
        if not closest_token_ce:
            raise ValueError("No valid CE token found.")

        closest_token_ce_str = str(closest_token_ce)

        # Fetch quotes for the closest CE token
        ce_result = api.get_quotes(exchange='NFO', token=closest_token_ce_str)

        print('ce result', ce_result)

        if not ce_result or 'tsym' not in ce_result:
            raise ValueError("Failed to fetch CE quotes.")
        trading_symbol_ce = ce_result['tsym']

        print('trading symbol second adjustment', trading_symbol_ce)

        # Place the CE order
        place_second_ce = place_order(
            buy_sell=const.SELL,
            quantity=const.QUANTITY,
            product_type=const.PRODUCT_TYPE_MIS,
            exchange=const.EXCHANGE_NFO,
            symbol=trading_symbol_ce,
            price_type=const.PRICE_TYPE_MARKET,
            trigger_price=None,
            price=0,
            remarks='second_ce'
        )
        if not place_second_ce or place_second_ce['stat'] != 'Ok':
            raise ValueError("Error placing second CE order.")

        time.sleep(3)

        # Check CE order status
        ce_order_status = api.single_order_history(
            orderno=place_second_ce['norenordno'])

        print('log 6 - ce order status', ce_order_status)

        if not ce_order_status or ce_order_status[0]['status'] != 'COMPLETE':
            raise ValueError("Second CE order not completed correctly.")

        ce_avg_price = float(ce_order_status[0]['avgprc'])

        print('second order ce avg price', ce_avg_price)
        if ce_avg_price <= 0:
            raise ValueError("Invalid average price for CE order.")

        # Calculate SL trigger and price
        ce_trigger_price = rounded_to_tick(ce_avg_price * 1.15)
        ce_price = rounded_to_tick(ce_avg_price * 1.3)

        print('log 7 - place new sl for new leg', ce_trigger_price, ce_price)

        # Place SL order for CE
        ce_sl_order = place_order(
            buy_sell=const.BUY,
            quantity=const.QUANTITY,
            product_type=const.PRODUCT_TYPE_MIS,
            exchange=const.EXCHANGE_NFO,
            symbol=trading_symbol_ce,
            price_type=const.PRICE_TYPE_SL_LMT,
            price=ce_price,
            trigger_price=ce_trigger_price,
            remarks='second_sl'
        )

        if not ce_sl_order or ce_sl_order['stat'] != 'Ok':
            raise ValueError("Error placing CE SL order.")

        # Trail the PE leg
        pe_trigger_price = rounded_to_tick(pe_ltp * 1.15)
        pe_price = rounded_to_tick(pe_ltp * 1.3)

        modify_pe_leg = api.modify_order(
            orderno=pe_sl_order_no,
            exchange=const.EXCHANGE_NFO,
            tradingsymbol=first_pe_strike,
            newtrigger_price=pe_trigger_price,
            newprice=pe_price,
            newprice_type=const.PRICE_TYPE_SL_LMT,
            newquantity=const.QUANTITY
        )

        print('log 9 - modify order', modify_pe_leg)

        if not modify_pe_leg or modify_pe_leg['stat'] != 'Ok':
            raise ValueError("Error modifying PE leg SL order.")

    except Exception as e:
        print(f"Exception occurred: {e}")
        universal_exit_triggered = True
        universel_exit()


def place_order_for_pe(ce_token):
    global universal_exit_triggered, ce_sl_order_no, first_ce_strike
    try:
        # Fetch the LTP value for CE
        print('ce token inside pe adjustment', ce_token)
        print('ce sl order no inside pe adj', ce_sl_order_no)
        ce_ltp = get_lp_value(ce_token)

        print('ce ltp for pe order placement', ce_ltp)
        if ce_ltp is None:
            raise ValueError("Failed to fetch LTP for CE token.")

        # Find the closest PE token based on LTP comparison
        closest_token_pe = min(
            PE_WEBSOCKET,
            key=lambda token: abs(
                float(PE_WEBSOCKET[token].get('lp', 0)) - float(ce_ltp)),
            default=None
        )
        if not closest_token_pe:
            raise ValueError("No valid PE token found.")

        closest_token_pe_str = str(closest_token_pe)

        # Fetch quotes for the closest PE token
        pe_result = api.get_quotes(exchange='NFO', token=closest_token_pe_str)

        if not pe_result or 'tsym' not in pe_result:
            raise ValueError("Failed to fetch PE quotes.")

        trading_symbol_pe = pe_result['tsym']

        print('trading symbol pe for placement', trading_symbol_pe)

        # Place the PE order
        place_second_pe = place_order(
            buy_sell=const.SELL,
            quantity=const.QUANTITY,
            product_type=const.PRODUCT_TYPE_MIS,
            exchange=const.EXCHANGE_NFO,
            symbol=trading_symbol_pe,
            price_type=const.PRICE_TYPE_MARKET,
            trigger_price=None,
            price=0,
            remarks='second_pe'
        )
        if not place_second_pe or place_second_pe['stat'] != 'Ok':
            raise ValueError("Error placing second PE order.")

        time.sleep(3)

        # Check PE order status
        pe_order_status = api.single_order_history(
            orderno=place_second_pe['norenordno'])

        if not pe_order_status or pe_order_status[0]['status'] != 'COMPLETE':
            raise ValueError("Second PE order not completed correctly.")

        pe_avg_price = float(pe_order_status[0]['avgprc'])
        if pe_avg_price <= 0:
            raise ValueError("Invalid average price for PE order.")

        # Calculate SL trigger and price
        pe_trigger_price = rounded_to_tick(pe_avg_price * 1.15)
        pe_price = rounded_to_tick(pe_avg_price * 1.3)

        # Place SL order for PE
        pe_sl_order = place_order(
            buy_sell=const.BUY,
            quantity=const.QUANTITY,
            product_type=const.PRODUCT_TYPE_MIS,
            exchange=const.EXCHANGE_NFO,
            symbol=trading_symbol_pe,
            price_type=const.PRICE_TYPE_SL_LMT,
            price=pe_price,
            trigger_price=pe_trigger_price,
            remarks='second_sl'
        )
        if not pe_sl_order or pe_sl_order['stat'] != 'Ok':
            raise ValueError("Error placing PE SL order.")

        # Trail the CE leg
        ce_trigger_price = rounded_to_tick(ce_ltp * 1.15)
        ce_price = rounded_to_tick(ce_ltp * 1.3)

        modify_ce_leg = api.modify_order(
            orderno=ce_sl_order_no,
            exchange=const.EXCHANGE_NFO,
            tradingsymbol=first_ce_strike,
            newtrigger_price=ce_trigger_price,
            newprice=ce_price,
            newprice_type=const.PRICE_TYPE_SL_LMT,
            newquantity=const.QUANTITY
        )
        if not modify_ce_leg or modify_ce_leg['stat'] != 'Ok':
            raise ValueError("Error modifying CE leg SL order.")

    except Exception as e:
        print(f"Exception occurred: {e}")
        universal_exit_triggered = True
        universel_exit()


def main():
    global first_order_placed, universal_exit_triggered, ce_token, pe_token, second_iteration_executed, CE_WEBSOCKET, PE_WEBSOCKET, second_sl_hit

    logging.basicConfig(
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        level=logging.INFO
    )

    class ShoonyaApiPy(NorenApi):
        def __init__(self):
            super().__init__(
                host='https://api.shoonya.com/NorenWClientTP/',
                websocket='wss://api.shoonya.com/NorenWSTP/'
            )
            global api
            api = self

    api = ShoonyaApiPy()

    login_status = api.login(
        userid=keys.USER_ID,
        password=keys.PASSWORD,
        twoFA=pyotp.TOTP(keys.TOTP_TOKEN).now(),
        vendor_code=keys.VENDOR_CODE,
        api_secret=keys.API_KEY_FINVASIA,
        imei=keys.IMEI
    )

    susertoken = login_status.get('susertoken')
    with open('token.txt', 'w') as file:
        file.write(susertoken)

    login_status = login_status.get(
        'stat') + "token =  " + login_status.get('susertoken')

    print('log 1 - user token:', login_status)

    nifty_ltp = get_ltp(const.EXCHANGE_NSE, const.NIFTY)

    atm_strike = get_atm_nifty(nifty_ltp['lp'])

    print('log 2 - atm strike : ', atm_strike)

    generate_and_get_strikes(atm_strike)

    print('here')

    token_lookup(pd.read_csv(const.OPEN_FNO_FILE),
                 CE_SYMBOL_LIST, PE_SYMBOL_LIST)


    def event_handler_feed_update(tick_data):
        global CE_WEBSOCKET, PE_WEBSOCKET, CE_TOKEN_LIST, PE_TOKEN_LIST

        if tick_data is not None and 'lp' in tick_data and 'tk' in tick_data:
            lp = float(tick_data['lp'])
        else:
            return

        try:
            token = int(tick_data['tk'])

            if token in CE_TOKEN_LIST:
                if token not in CE_WEBSOCKET:
                    CE_WEBSOCKET[token] = {'tk': token, 'lp': lp}
                else:
                    CE_WEBSOCKET[token]['lp'] = lp

            elif token in PE_TOKEN_LIST:
                if token not in PE_WEBSOCKET:
                    PE_WEBSOCKET[token] = {'tk': token, 'lp': lp}
                else:
                    PE_WEBSOCKET[token]['lp'] = lp


        except Exception as e:
            print(f"Error updating dictionary for token {tick_data['tk']}: {e}")

    def event_handler_order_update(tick_data):
        global sl_not_placed_correctly, ce_first_sl_hit, pe_first_sl_hit, second_sl_hit
        # print(f"feed update, SYMBOL - {tick_data.get('tsym', 'None')}, TYPE - {tick_data.get('trantype', 'None')}, ORDER TYPE - {tick_data.get('prctyp', 'None')}, QTY - {tick_data.get('qty', 'None')}, STATUS - {tick_data.get('status', 'None')}, PRICE - {tick_data.get('avgprc', 'None')}, REMARKS - {tick_data.get('remarks', 'None')}")

        # SL order rejected
        if 'remarks' in tick_data and tick_data['remarks'] == 'ce_first_sl' and 'status' in tick_data and tick_data['status'] == 'REJECTED':
            sl_not_placed_correctly = True
            print(f"PE Sl is rejected {tick_data['rejreason']}")
        if 'remarks' in tick_data and tick_data['remarks'] == 'pe_first_sl' and 'status' in tick_data and tick_data['status'] == 'REJECTED':
            sl_not_placed_correctly = True
            print(f"CE Sl is rejected {tick_data['rejreason']}")
        
        if 'remarks' in tick_data and tick_data['remarks'] == 'second_sl' and 'status' in tick_data and tick_data['status'] == 'REJECTED':
            sl_not_placed_correctly = True
            print(f"Second Sl is rejected {tick_data['rejreason']}")

        # completed scenarios
        if 'remarks' in tick_data and tick_data['remarks'] == 'ce_first_sl' and 'status' in tick_data and tick_data['status'] == 'COMPLETE':
            ce_first_sl_hit = True
            print('CE Sl is hit')

        if 'remarks' in tick_data and tick_data['remarks'] == 'pe_first_sl' and 'status' in tick_data and tick_data['status'] == 'COMPLETE':
            pe_first_sl_hit = True
            print('PE Sl is hit')

        if 'remarks' in tick_data and tick_data['remarks'] == 'second_sl' and 'status' in tick_data and tick_data['status'] == 'COMPLETE':
            second_sl_hit = True
            print('Second Sl is hit')

    def open_callback():
        global feed_opened
        feed_opened = True

    api.start_websocket(order_update_callback=event_handler_order_update,
                        subscribe_callback=event_handler_feed_update,
                        socket_open_callback=open_callback)
    while (feed_opened == False):
        pass

    tokens_subscribe_ce = [f'NFO|{strike}' for strike in CE_TOKEN_LIST]
    tokens_subscribe_pe = [f'NFO|{strike}' for strike in PE_TOKEN_LIST]

    total_tokens_subscribe = tokens_subscribe_ce + tokens_subscribe_pe
    print(total_tokens_subscribe, 'subscrbe tokens')
    res = api.subscribe(total_tokens_subscribe)
    print(res, 'sup')

    while True:
        now = datetime.datetime.now().time()
        target_time = datetime.time(9, 30, 0)


        if universal_exit_triggered == True:
            print(
                'universal exit is triggered, all positions will be closed and program will be stopped.')
            break

        if sl_not_placed_correctly == True:
            print('SL is not placed correctly check the positions')
            break

        if now >= target_time and not first_order_placed:
            print('Placing first order...')
            place_straddle_and_sl()
            first_order_placed = True

        if ce_first_sl_hit == True and pe_first_sl_hit == False and second_iteration_executed == False:
            second_iteration_executed = True
            place_order_for_ce(pe_token)

        if ce_first_sl_hit == False and pe_first_sl_hit == True and second_iteration_executed == False:
            second_iteration_executed = True
            place_order_for_pe(ce_token)

        if (ce_first_sl_hit and pe_first_sl_hit and second_iteration_executed) or (second_sl_hit and second_iteration_executed):
            print('Second iteration SL is hit, closing and exiting all positions.')
            universel_exit()
            break

        time.sleep(5)


if __name__ == "__main__":
    file_url = const.FINVASIA_NFO_URL
    file_name = const.SAVE_ZIP_FILE_NAME
    download_file(file_url, file_name)

    with zipfile.ZipFile(file_name, 'r') as zip_file:
        zip_file.extractall()

    main()
