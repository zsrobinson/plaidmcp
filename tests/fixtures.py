from store import connect, ingest_accounts, ingest_transactions, ingest_holdings, publish
AS_OF='2026-11-10T12:00:00+00:00'
MERCHANT='SECRET FIXTURE MERCHANT'
def seed(private, public):
    db=connect(private)
    db.executemany('INSERT INTO items VALUES(?,?,?,NULL)',[('bank','Example Bank',AS_OF),('broker','Example Broker',AS_OF)])
    def account(id,name,type,current,subtype,mask):
        return {'account_id':id,'name':name,'type':type,'subtype':subtype,'mask':mask,'balances':{'current':current,'available':current if type=='depository' else None,'limit':5000 if type=='credit' else None,'iso_currency_code':'USD'}}
    ingest_accounts(db,'bank',[account('check','Checking','depository',2000,'checking','1234'),account('card','Credit','credit',200,'credit card','5678')])
    ingest_accounts(db,'broker',[account('invest','Brokerage','investment',3000,'brokerage','9012')])
    def tx(id,date,amount,primary,detail,pending=False):
        return {'transaction_id':id,'account_id':'check','date':date,'amount':amount,'personal_finance_category':{'primary':primary,'detailed':detail},'pending':pending,'merchant_name':MERCHANT,'name':'PRIVATE DESCRIPTION','counterparties':[{'name':'PRIVATE COUNTERPARTY'}],'iso_currency_code':'USD'}
    ingest_transactions(db,'bank',{'added':[
        tx('1','2026-10-10',80,'FOOD_AND_DRINK','FOOD_AND_DRINK_GROCERIES'),
        tx('2','2026-11-03',100,'FOOD_AND_DRINK','FOOD_AND_DRINK_GROCERIES'),
        tx('3','2026-11-04',20,'FOOD_AND_DRINK','FOOD_AND_DRINK_GROCERIES'),
        tx('4','2026-11-04',-10,'FOOD_AND_DRINK','FOOD_AND_DRINK_GROCERIES'),
        tx('5','2026-11-01',-1000,'INCOME','INCOME_WAGES'),
        tx('6','2026-11-01',500,'TRANSFER_OUT','TRANSFER_OUT_ACCOUNT_TRANSFER'),
        tx('7','2026-11-01',200,'LOAN_PAYMENTS','LOAN_PAYMENTS_CREDIT_CARD_PAYMENT'),
        tx('8','2026-11-01',999,'FOOD_AND_DRINK','FOOD_AND_DRINK_GROCERIES',True)]})
    ingest_holdings(db,'broker',{'securities':[{'security_id':'sec','ticker_symbol':'FAKE','name':'Fixture Fund','type':'etf'}], 'holdings':[{'account_id':'invest','security_id':'sec','quantity':30,'institution_value':3000,'cost_basis':2500,'iso_currency_code':'USD'}]})
    db.commit(); publish(db,public)
    return db
