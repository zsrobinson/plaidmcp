"""Owned financial categories shared by sources and read-only discovery."""
CATEGORIES = {
 'food': ('Food and drink', 'groceries','dining','other_food'),
 'transport': ('Transport','fuel','transit','other_transport'),
 'shopping': ('Shopping','general_shopping'),
 'travel': ('Travel','general_travel'),
 'personal_care': ('Personal care','general_personal_care'),
 'housing': ('Housing and utilities','rent','utilities','other_housing'),
 'health': ('Health','general_health'),
 'entertainment': ('Entertainment','general_entertainment'),
 'income': ('Income','wages','interest','other_income'),
 'fees': ('Fees','general_fees'),
 'loan_payments': ('Loan payments','general_loan_payments'),
 'bank_transfer': ('Bank transfers and card payments','bank_transfer'),
 'peer_payment': ('Peer payments','peer_sent','peer_received'),
 'other': ('Other or unclassified','unclassified'),
}
PRIMARY = {'FOOD_AND_DRINK':'food','TRANSPORTATION':'transport','GENERAL_MERCHANDISE':'shopping',
 'TRAVEL':'travel','PERSONAL_CARE':'personal_care','RENT_AND_UTILITIES':'housing','MEDICAL':'health',
 'ENTERTAINMENT':'entertainment','INCOME':'income','BANK_FEES':'fees','LOAN_PAYMENTS':'loan_payments'}

def category(primary,detail):
    if detail in ('TRANSFER_OUT_PEER_TRANSFER','TRANSFER_IN_PEER_TRANSFER'): return 'peer_payment'
    if primary in ('TRANSFER_IN','TRANSFER_OUT') or detail=='LOAN_PAYMENTS_CREDIT_CARD_PAYMENT': return 'bank_transfer'
    return PRIMARY.get(primary,'other')


def detailed(primary,detail):
    cat=category(primary,detail)
    if cat=='peer_payment': return 'peer_received' if primary=='TRANSFER_IN' else 'peer_sent'
    known={'FOOD_AND_DRINK_GROCERIES':'groceries','INCOME_WAGES':'wages','INCOME_INTEREST_EARNED':'interest',
        'TRANSPORTATION_GAS':'fuel','RENT_AND_UTILITIES_RENT':'rent'}
    return known.get(detail,CATEGORIES[cat][-1])
