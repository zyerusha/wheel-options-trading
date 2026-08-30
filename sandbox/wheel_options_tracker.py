# wheel_options_tracker.py
import pandas as pd
import numpy as np
import uuid
import re
import os



class WheelNumberGenerator:
    """
    Guarantees that a symbol belongs to exactly one wheel per ticker.
    CALL-START can reuse earliest inactive PUT wheel.
    """

    def __init__(self):
        self._next = {}           # next wheel number per ticker
        self._wheels = {}         # wheels[ticker][type] -> list[dict]
        self._symbol_index = {}   # (ticker, type, symbol) -> (year, wheel_number)

    def _ensure(self, ticker: str, type: str):
        self._next.setdefault(ticker, 1)
        self._wheels.setdefault(ticker, {})
        self._wheels[ticker].setdefault(type, [])

    # ---------------- Wheel lifecycle ----------------

    def start_wheel(self, ticker: str, year: int, type: str, symbol: str) -> str:
        self._ensure(ticker, type)
        key = (ticker, type, symbol)
        if key in self._symbol_index:
            y, n = self._symbol_index[key]
            return f"{ticker}-{y}-{n}"

        n = self._next[ticker]
        self._next[ticker] += 1

        wheel = {
            'number': n,
            'year': year,
            'symbols': {symbol},
            'symbol_pairs': set(),
            'active': True
        }

        self._wheels[ticker][type].append(wheel)
        self._symbol_index[key] = (year, n)
        return f"{ticker}-{year}-{n}"

    def clear_active(self, ticker: str, wheel_number: int, type: str):
        self._ensure(ticker, type)
        for w in self._wheels[ticker][type]:
            if w['number'] == wheel_number:
                w['active'] = False
                return

    # ---------------- Registration helpers ----------------

    def _claim_symbol(self, ticker, type, symbol, year, wheel_number):
        key = (ticker, type, symbol)
        if key not in self._symbol_index:
            self._symbol_index[key] = (year, wheel_number)
            return True
        return self._symbol_index[key] == (year, wheel_number)

    def register_symbol(self, ticker, year, type, wheel_number, symbol):
        self._ensure(ticker, type)
        if not self._claim_symbol(ticker, type, symbol, year, wheel_number):
            return
        for w in self._wheels[ticker][type]:
            if w['number'] == wheel_number and w['year'] == year:
                w['symbols'].add(symbol)
                return

    def register_roll_pair(self, ticker, year, type, wheel_number, old_symbol, new_symbol):
        self._ensure(ticker, type)
        # Determine authoritative wheel
        for sym in (old_symbol, new_symbol):
            key = (ticker, type, sym)
            if key in self._symbol_index:
                year, wheel_number = self._symbol_index[key]
                break
        pair = frozenset({old_symbol, new_symbol})
        for w in self._wheels[ticker][type]:
            if w['number'] == wheel_number and w['year'] == year:
                for sym in pair:
                    self._claim_symbol(ticker, type, sym, year, wheel_number)
                w['symbols'].update(pair)
                w['symbol_pairs'].add(pair)
                return

    # ---------------- Wheel resolution ----------------

    def get_current_wheel_id(self, ticker: str, year: int, type: str, symbol: str, paired_symbol: str | None = None) -> str:
        self._ensure(ticker, type)
        key = (ticker, type, symbol)
        if key in self._symbol_index:
            y, n = self._symbol_index[key]
            return f"{ticker}-{y}-{n}"

        # paired symbol ownership
        if paired_symbol:
            pkey = (ticker, type, paired_symbol)
            if pkey in self._symbol_index:
                y, n = self._symbol_index[pkey]
                self._claim_symbol(ticker, type, symbol, y, n)
                return f"{ticker}-{y}-{n}"

        # most recent active wheel
        for w in reversed(self._wheels[ticker][type]):
            if w['active']:
                self._claim_symbol(ticker, type, symbol, w['year'], w['number'])
                w['symbols'].add(symbol)
                if paired_symbol:
                    self._claim_symbol(ticker, type, paired_symbol, w['year'], w['number'])
                    w['symbols'].add(paired_symbol)
                    w['symbol_pairs'].add(frozenset({symbol, paired_symbol}))
                return f"{ticker}-{w['year']}-{w['number']}"

        # start new wheel
        return self.start_wheel(ticker, year, type, symbol)

    # ---------------- Reuse earliest inactive PUT wheel for CALL-START ----------------
    def get_wheel_for_call_start(self, ticker, year, symbol) -> str:
        self._ensure(ticker, 'PUT')
        for w in self._wheels[ticker]['PUT']:
            if not w['active']:
                # Claim CALL symbol into this PUT wheel
                self._claim_symbol(ticker, 'CALL', symbol, w['year'], w['number'])
                w['symbols'].add(symbol)
                return f"{ticker}-{w['year']}-{w['number']}"
        # fallback: start new CALL wheel if no PUT wheel available
        return self.start_wheel(ticker, year, 'CALL', symbol)

 
class RollManager:
    """
    Handles BTC ↔ STO roll detection and registration
    against WheelNumberGenerator.
    """

    def __init__(self, wheel_gen, ticker, sub_df):
        self.wheel_gen = wheel_gen
        self.ticker = ticker
        self.sub_df = sub_df

    def _row(self, rid):
        return self.sub_df.loc[self.sub_df['Row ID'] == rid].iloc[0]

    def process_rolls(
        self,
        sto_rids,
        btc_rids,
        option_type,   # 'PUT' or 'CALL'
        sto_key,
        btc_key
    ):
        """
        Process aligned STO/BTC roll pairs.
        """
        for sto_rid, btc_rid in zip(sto_rids, btc_rids):

            sto_row = self._row(sto_rid)
            btc_row = self._row(btc_rid)

            year = sto_row['Entry Date'].year
            symbol_sto = sto_row['Symbol']
            symbol_btc = btc_row['Symbol']

            # Resolve wheel using roll pair
            wheel_id = self.wheel_gen.get_current_wheel_id(
                self.ticker,
                year,
                option_type,
                symbol=symbol_sto,
                paired_symbol=symbol_btc
            )

            wheel_number = int(wheel_id.split('-')[-1])

            # Explicitly register the roll pair
            self.wheel_gen.register_roll_pair(
                self.ticker,
                year,
                option_type,
                wheel_number,
                symbol_btc,
                symbol_sto
            )

            # Tag STO leg
            self.sub_df.loc[
                self.sub_df['Row ID'] == sto_rid,
                ['Key', 'Used', 'Wheel ID']
            ] = [sto_key, True, wheel_id]

            # Tag BTC leg
            self.sub_df.loc[
                self.sub_df['Row ID'] == btc_rid,
                ['Key', 'Used', 'Wheel ID']
            ] = [btc_key, True, wheel_id]    

# ==============================================================
# 0. CONFIG
# ==============================================================

input_file = "History_for_Account_14062026.csv"    # Your broker CSV
output_file = "wheel_full_enriched.csv"             # Output file

def clear():
    os.system('cls' if os.name == 'nt' else 'clear')


def print_df(df):
    df = df.dropna(subset=['Entry Date'])

    desired_order = [
        'Row ID',
        'Used',
        'Entry Date',
        'Ticker',
        'Wheel ID',
        'Key',
        # 'Node ID',
        'Qty',
        'Expiration Date',
        'Strike',
        'Type',
        'Price',
        'Commission',
        'Premium Rx',
        'Status',
        'Symbol'#,
        # 'Assigned Date',
        # 'Settlement Date',
        # 'Qty',
        # 'Net Credit',
        # 'Days in Trade'
    ]

    df = df[[c for c in desired_order if c in df.columns]]


    # ==============================================================
    # 9. FORMAT DATES AS MM/DD/YYYY
    # ==============================================================

    for col in ['Entry Date', 'Settlement Date', 'Expiration Date', 'Assigned Date']:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors='coerce').dt.strftime('%m/%d/%Y')

    print(df)

    return df    
# ==============================================================
# 1. HEADER NORMALIZATION
#    Broker file uses incorrect headers — fix them first.
# ==============================================================

def rename_headers(df):
    """
    Rename broker-provided incorrect column names into a consistent
    internal naming structure. DO NOT CHANGE—this mapping is intentional
    because the original CSV labels are wrong.
    """
    rename_map = {
        'Run Date': 'org_entry_date',
        'Action': 'orig_action',
        'Symbol': 'orig_symbol',
        'Description': 'orig_description',
        'Type': 'org_cash_margin',
        'Quantity': 'org_price',               # (BROKER mislabels Quantity as price)
        'Price ($)': 'org_contracts',          # (BROKER mislabels Price as contracts)
        'Commission ($)': 'orig_commission',
        'Fees ($)': 'orig_fees',
        'Accrued Interest ($)': 'org_accrued_interest',
        'Amount ($)': 'org_premium_rx',
        'Cash Balance ($)': 'org_cash_balance',
        'Settlement Date': 'org_settlement_date'
    }
    return df.rename(columns=rename_map)


clear()
df = pd.read_csv(input_file)
df = rename_headers(df)
# Add Row_ID
df = df.reset_index(drop=True)
df['Row ID'] = df.index

# ==============================================================
# 2. DATE PARSING
# ==============================================================

df['Entry Date'] = pd.to_datetime(df['org_entry_date'], errors='coerce')
df['Settlement Date'] = pd.to_datetime(df['org_settlement_date'], errors='coerce')


# ==============================================================
# 3. OPTION FIELD EXTRACTION
# ==============================================================

def extract_option_fields(row):
    """
    Parse OCC option symbol:  -AAPL250117C190
    Breakdown:
        AAPL     = ticker
        25 01 17 = YY MM DD
        C        = Call
        190      = Strike
    Also extract price, contracts, qty, commissions, premium.
    """
    ticker = expiration = strike = opt_type = None
    qty = price = commission = premium = contracts = None

    symbol = row.get('orig_symbol')

    if isinstance(symbol, str):
        symbol = symbol.strip()

        match = re.match(r'-?([A-Z]+)(\d{2})(\d{2})(\d{2})([CP])(\d+(\.\d+)?)', symbol)
        if match:
            ticker = match.group(1)
            yy, mm, dd = match.group(2), match.group(3), match.group(4)

            expiration = pd.to_datetime(f"{yy}-{mm}-{dd}",
                                        format='%y-%m-%d',
                                        errors='coerce')

            opt_type = 'CALL' if match.group(5) == 'C' else 'PUT'
            strike = float(match.group(6))

    # Extract contracts count (mis-labeled by broker)
    try:
        contracts = float(row.get('org_contracts', 0))
    except:
        contracts = None

    # Quantity = contracts * 100
    try:
        qty = abs(float(row.get('org_contracts', 0))) * 100
    except:
        qty = None

    # Price per share
    try:
        price = float(row.get('org_price', 0))
    except:
        price = None

    # Premium received
    try:
        premium = float(row.get('org_premium_rx', 0))
    except:
        premium = None

    # Commission = commission + fees
    try:
        commission = float(row.get('orig_commission', 0)) + float(row.get('orig_fees', 0))
    except:
        commission = None

    return pd.Series({
        'Ticker': ticker,
        'Expiration Date': expiration,
        'Strike': strike,
        'Type': opt_type,
        'Qty': qty,
        'Commission': commission,
        'Price': price,
        'Premium Rx': premium,
        'Contracts': contracts
    })


df[['Ticker','Expiration Date','Strike','Type','Qty','Commission','Price','Premium Rx','Contracts']] = (
    df.apply(extract_option_fields, axis=1)
)

# ==============================================================
# 4. PARSE STATUS (BUY/SELL/OPEN/CLOSE/CALL/PUT)
# ==============================================================

def parse_option_status(action):
    """
    Extracts structured status information from raw action text:
        SOLD OPENING CALL
        BOUGHT CLOSING PUT
        EXPIRED
        ASSIGNED
    Similar to your Sheets formula.
    """
    if not isinstance(action, str) or action.strip() == '':
        return None

    text = action.upper()

    verb = re.search(r'\b(SOLD|BOUGHT|EXPIRED|ASSIGNED)\b', text)
    oc   = re.search(r'\b(OPENING|CLOSING)\b', text)
    t    = re.search(r'\b(CALL|PUT)\b', text)

    parts = [
        verb.group(1) if verb else '',
        oc.group(1) if oc else '',
        t.group(1) if t else ''
    ]

    parts = [p for p in parts if p]     # filter empty
    return ' '.join(parts) if parts else None


df['Status'] = df['orig_action'].apply(parse_option_status)


# ==============================================================
# 5. COMPUTE METRICS (Net Credit + Days Held)
# ==============================================================

def compute_metrics(row):
    # Net Credit = Price × Qty − Commissions
    net_credit = None
    if pd.notnull(row['Qty']) and pd.notnull(row['Price']):
        net_credit = row['Price'] * row['Qty'] - (row['Commission'] or 0)

    # Days in trade
    days = None
    if pd.notnull(row['Entry Date']) and pd.notnull(row['Settlement Date']):
        days = (row['Settlement Date'] - row['Entry Date']).days

    return pd.Series([net_credit, days], index=['Net Credit', 'Days in Trade'])


df[['Net Credit','Days in Trade']] = df.apply(compute_metrics, axis=1)


# ==============================================================
# 6. EXTRACT ASSIGNED/EXPIRED DATE FROM DESCRIPTION
# ==============================================================

def extract_date_from_description(text):
    """
    Detects dates inside description:
      'as of Nov-21-2025'
      'NOV 21 25'
    Returns a datetime or None.
    """
    if not isinstance(text, str):
        return None

    # "as of Nov-21-2025"
    m = re.search(r'as of (\w+-\d{2}-\d{4})', text)
    if m:
        return pd.to_datetime(m.group(1), format='%b-%d-%Y', errors='coerce')

    # "NOV 21 25"
    m2 = re.search(r'([A-Z]{3} \d{2} \d{2})', text)
    if m2:
        return pd.to_datetime(m2.group(1), format='%b %d %y', errors='coerce')

    return None


df['Assigned Date'] = df['orig_description'].apply(extract_date_from_description)


# ==============================================================
# 7. CLEAN SYMBOL
# ==============================================================

df['Symbol'] = df['orig_symbol'].astype(str).str.replace('-', '').str.strip()


def find_common_ids(a_df, b_df, common_col, ret_col):
    # set of Symbols present in a_df and b_df intersection
    common = set(a_df[common_col].dropna().unique()) & set(b_df[common_col].dropna().unique())

    if common:
        a_ids_with_any_match = a_df[a_df[common_col].isin(common)][ret_col]
        b_ids_with_any_match = b_df[b_df[common_col].isin(common)][ret_col]
    else:
        a_ids_with_any_match = []
        b_ids_with_any_match = []

    return [a_ids_with_any_match, b_ids_with_any_match]
      
def find_commons(a_df, b_df, key_cols, ret_col, key_transform=None, dropna=True):

    if isinstance(key_cols, str):
        key_cols = [key_cols]
    key_transform = key_transform or {}

    # Work on copies
    a = a_df.copy()
    b = b_df.copy()

    # Create normalized columns for each key
    norm_cols = []
    for k in key_cols:
        norm_col = f"_key__{k.replace(' ', '_')}"
        norm_cols.append(norm_col)
        transform = key_transform.get(k)

        if transform:
            # apply transform safely (handle exceptions by mapping to None)
            a[norm_col] = a[k].apply(lambda v: (transform(v) if pd.notna(v) else None) if k in a.columns else None)
            b[norm_col] = b[k].apply(lambda v: (transform(v) if pd.notna(v) else None) if k in b.columns else None)
        else:
            # no transform: copy values (keep NaNs)
            a[norm_col] = a[k] if k in a.columns else pd.NA
            b[norm_col] = b[k] if k in b.columns else pd.NA

    # Build tuple keys (None if any component is NA)
    def _row_key(row):
        parts = []
        for c in norm_cols:
            v = row.get(c)
            if pd.isna(v):
                return None
            parts.append(v)
        return tuple(parts)

    a_keys = a.apply(_row_key, axis=1)
    b_keys = b.apply(_row_key, axis=1)

    if dropna:
        a_set = set(a_keys.dropna().unique())
        b_set = set(b_keys.dropna().unique())
    else:
        a_set = set(a_keys.unique())
        b_set = set(b_keys.unique())

    common = a_set & b_set
    if not common:
        return [], []

    a_vals = a.loc[a_keys.isin(common), ret_col].tolist()
    b_vals = b.loc[b_keys.isin(common), ret_col].tolist()

    return a_vals, b_vals
  
def get_row(df, rid):
    return df.loc[df['Row ID'] == rid].iloc[0]

def tag_wheels(df):


    df = df.reset_index(drop=False).rename(columns={'index': '_orig_index'})
    df = df.sort_values(['Entry Date', '_orig_index']).reset_index(drop=True)
    df['Wheel ID'] = None  # clear any existing wheel ID
    df['Key'] = None
    df['Used'] = False
    ticker_array = df['Ticker'].unique()
    
    for ticker in ticker_array:  

        is_long_call = (df['Type'] == 'CALL') & (df['Wheel ID'].isna() | df['Wheel ID'].eq(''))
        is_long_put = (df['Type'] == 'PUT') & (df['Wheel ID'].isna() | df['Wheel ID'].eq(''))

        df.loc[is_long_call, 'Wheel ID'] = 'LONG CALL'
        df.loc[is_long_call, 'Key'] = 'LONG CALL'

        df.loc[is_long_put, 'Wheel ID'] = 'LONG PUT'
        df.loc[is_long_put, 'Key'] = 'LONG PUT'

        df_wheel_candidates = df[~df['Wheel ID'].isin(['LONG CALL', 'LONG PUT'])]

        mask = df['Ticker'] == ticker
        sub_df = df.loc[mask].copy()

        node_symbol = None  # indication of chain
        wheel_id_counter = 1
        
        
        wheel_gen = WheelNumberGenerator()
                
        sold_opening_put_df = sub_df[sub_df['Status'].str.startswith('SOLD OPENING PUT', na=False)]
        bought_opening_put_df = sub_df[sub_df['Status'].str.startswith('BOUGHT OPENING PUT', na=False)]
        sold_closing_put_df = sub_df[sub_df['Status'].str.startswith('SOLD CLOSING PUT', na=False)]
        bought_closing_put_df = sub_df[sub_df['Status'].str.startswith('BOUGHT CLOSING PUT', na=False)]
        
        sold_opening_call_df = sub_df[sub_df['Status'].str.startswith('SOLD OPENING CALL', na=False)]
        bought_opening_call_df = sub_df[sub_df['Status'].str.startswith('BOUGHT OPENING CALL', na=False)]
        sold_closing_call_df = sub_df[sub_df['Status'].str.startswith('SOLD CLOSING CALL', na=False)]
        bought_closing_call_df = sub_df[sub_df['Status'].str.startswith('BOUGHT CLOSING CALL', na=False)]
        
        assigned_put_df = sub_df[sub_df['Status'].str.startswith('ASSIGNED PUT', na=False)]
        assigned_call_df = sub_df[sub_df['Status'].str.startswith('ASSIGNED CALL', na=False)]
        
        expired_put_df = sub_df[sub_df['Status'].str.startswith('EXPIRED PUT', na=False)]
        expired_call_df = sub_df[sub_df['Status'].str.startswith('EXPIRED CALL', na=False)] 
                
        [put_sto_matches, put_btc_matches] = find_commons(sold_opening_put_df, bought_closing_put_df, "Symbol", "Row ID")
        [call_sto_matches, call_btc_matches] = find_commons(sold_opening_call_df, bought_closing_call_df, "Symbol", "Row ID")
        
        # [bad_put_sto_matches, bad_put_btc_matches] = find_commons(sold_opening_put_df, bought_closing_put_df, "Entry Date", "Row ID")
        # [bad_call_sto_matches, bad_call_btc_matches] = find_commons(sold_opening_call_df, bought_closing_call_df, "Entry Date", "Row ID")
        
        
        [put_roll_sto_matches, put_roll_btc_matches] = find_commons(sold_opening_put_df, bought_closing_put_df, {"Entry Date", "Qty"}, "Row ID")
        [call_roll_sto_matches, call_roll_btc_matches] = find_commons(sold_opening_call_df, bought_closing_call_df, {"Entry Date", "Qty"}, "Row ID")
        [put_assigned_sto_matches, put_assigned_matches] = find_commons(sold_opening_put_df, assigned_put_df, {"Symbol", "Qty"}, "Row ID")
        [call_assigned_sto_matches, call_assigned_matches] = find_commons(sold_opening_call_df, assigned_call_df, {"Symbol", "Qty"}, "Row ID")



        roll_mgr = RollManager(wheel_gen, ticker, sub_df)
        roll_mgr.process_rolls(
            put_roll_sto_matches,
            put_roll_btc_matches,
            option_type='PUT',
            sto_key='PUT-STO',
            btc_key='PUT-BTC'
        )

        roll_mgr.process_rolls(
            call_roll_sto_matches,
            call_roll_btc_matches,
            option_type='CALL',
            sto_key='CALL-STO',
            btc_key='CALL-BTC'
        )

        # for rid in put_roll_sto_matches:
        #     # if rid not in bad_put_sto_matches:
        #     sub_df.loc[sub_df['Row ID'] == rid, 'Key'] = "PUT-STO"
        #     sub_df.loc[sub_df['Row ID'] == rid, 'Used'] = True
        # for rid in put_roll_btc_matches:
        #     # if rid not in bad_put_btc_matches:
        #     sub_df.loc[sub_df['Row ID'] == rid, 'Key'] = "PUT-BTC"
        #     sub_df.loc[sub_df['Row ID'] == rid, 'Used'] = True
        # for rid in call_roll_sto_matches:
        #     # if rid not in bad_call_sto_matches:
        #     sub_df.loc[sub_df['Row ID'] == rid, 'Key'] = "CALL-STO"
        #     sub_df.loc[sub_df['Row ID'] == rid, 'Used'] = True
        # for rid in call_roll_btc_matches:
        #     # if rid not in bad_call_btc_matches:
        #     sub_df.loc[sub_df['Row ID'] == rid, 'Key'] = "CALL-BTC"
        #     sub_df.loc[sub_df['Row ID'] == rid, 'Used'] = True
        for rid in put_assigned_matches:
            sub_df.loc[sub_df['Row ID'] == rid, 'Key'] = "PUT-END"    
        for rid in call_assigned_matches:
            sub_df.loc[sub_df['Row ID'] == rid, 'Key'] = "CALL-END"

        
        
        
        for i, row in sub_df.iterrows():  
            rid = row.get('Row ID')
            entry_dt = row.get('Entry Date')
            year = entry_dt.year if pd.notnull(entry_dt) else 'XXXX'
            status = str(row.get('Status') or '')
            key = str(row.get('Key') or '')
            type = str(row.get('Type') or '')
            contracts = row.get('Qty')
            symbol = row.get('Symbol')
            used = row.get('Used') 
            
            # wheel_number = int(wheel_id.split('-')[-1])
            # wheel_gen.register_roll_pair(
            #     ticker,
            #     year,
            #     type,
            #     wheel_number,
            #     old_symbol,
            #     new_symbol
            # )
                                
            if (used == False) and status.startswith('SOLD OPENING'):
                wheel_id_str = None
                if type == 'CALL':                  
                    wheel_id_str = wheel_gen.get_wheel_for_call_start(ticker, year, symbol)
                else:
                    wheel_id_str = wheel_gen.start_wheel(ticker, year, type, symbol)
                
                sub_df.loc[sub_df['Row ID'] == rid, 'Used'] = True

                # Determine START vs MORE
                existing_rows_with_wheel = sub_df[sub_df['Wheel ID'] == wheel_id_str]
                # if existing_rows_with_wheel.empty:
                key_val = f"{type}-START"
                

                sub_df.loc[sub_df['Row ID'] == rid, 'Key'] = key_val
                sub_df.loc[sub_df['Row ID'] == rid, 'Wheel ID'] = wheel_id_str

                # propagate to previous rows if needed
                current_used = sub_df['Used'].astype(bool).to_numpy()
                pos = np.flatnonzero(~current_used)
                df_before_used = sub_df.iloc[: pos[0]] if pos.size else sub_df

                for i, prev_row in df_before_used.iterrows(): 
                    prev_wheel_id  = prev_row.get('Wheel ID') 
                    prev_rid = prev_row.get('Row ID') 
                    if prev_wheel_id is None:
                        sub_df.loc[sub_df['Row ID'] == prev_rid, 'Wheel ID'] = wheel_id_str
                                 
            elif (used == False) and status.startswith('ASSIGNED'):
                wheel_id = wheel_gen.get_current_wheel_id(ticker, year, type, symbol)
                wheel_number = int(wheel_id.split('-')[-1])
                wheel_gen.clear_active(ticker, wheel_number, type)
                sub_df.loc[sub_df['Row ID'] == rid, 'Used'] = True
                sub_df.loc[sub_df['Row ID'] == rid, 'Key'] = f"{type}-END"
                sub_df.loc[sub_df['Row ID'] == rid, 'Wheel ID'] = wheel_id                  
                    
            elif (used == False) and status.startswith('EXPIRED'):
                sub_df.loc[sub_df['Row ID'] == rid, 'Used'] = True
                sub_df.loc[sub_df['Row ID'] == rid, 'Key'] = f"{type}-EXP"     
                sub_df.loc[sub_df['Row ID'] == rid, 'Wheel ID'] = wheel_gen.get_current_wheel_id(ticker, year, type, symbol)

            else:
                sub_df.loc[sub_df['Row ID'] == rid, 'Wheel ID'] = wheel_gen.get_current_wheel_id(ticker, year, type, symbol)

        df.loc[mask] = sub_df
        # print_df(sub_df)
        # print("\n")
        
    return df


# df = df[df['Ticker'] == 'GLD']
# print_df(df)
df = tag_wheels(df)


df = print_df(df)
df.to_csv(output_file, index=False)
print(f"Full Wheel dataset saved to {output_file}")

# Group by ticker and sum Premium Rx
ticker_sums = df.groupby('Ticker')['Premium Rx'].sum()
# Print per ticker
for ticker, total in ticker_sums.items():
    print(f"{ticker} Premium Rx: ${total:2.2f}")
    
total_premium = df['Premium Rx'].sum()
print(f"Total Premium Rx: ${total_premium:2.2f}")