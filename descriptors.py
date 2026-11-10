"""Conservative private-only descriptor classification. Never return cleaned free text."""
import re
import unicodedata

# Generic public brands/products, not institution-specific observed descriptors.
BRANDS={
 'ADOBE':('Adobe',{'CREATIVE','CLOUD','ACROBAT'}),
 'MICROSOFT':('Microsoft',{'MSFT','OFFICE','AZURE'}),
 'GITHUB':('GitHub',{'COPILOT'}),
 'GOOGLE':('Google',{'GOOGLE','WORKSPACE','CLOUD','STORAGE'}),
 'DROPBOX':('Dropbox',{'PLUS','BUSINESS'}),
 'NETFLIX':('Netflix',set()),'SPOTIFY':('Spotify',{'PREMIUM'}),
 'ANTHROPIC':('Anthropic',{'CLAUDE','SUBSCRIPTION','PRO','MAX'}),
 'OPENAI':('OpenAI',{'CHATGPT','CODEX','SUBSCRIPTION','PLUS','PRO','BUSINESS'}),
}
REPORTABLE_SOFTWARE={'Adobe','Microsoft','GitHub','Google','Dropbox','Anthropic','OpenAI'}
FIRST_NAMES=set('JOHN JANE JAMES ROBERT MICHAEL WILLIAM DAVID MARY PATRICIA JENNIFER LINDA ELIZABETH BARBARA SUSAN JESSICA SARAH THOMAS CHARLES DANIEL MATTHEW ANTHONY MARK DONALD STEVEN PAUL ANDREW JOSHUA KENNETH KEVIN BRIAN GEORGE TIMOTHY RONALD EDWARD JASON JEFFREY RYAN JACOB GARY NICHOLAS ERIC JONATHAN STEPHEN LARRY JUSTIN SCOTT BRANDON BENJAMIN SAMUEL GREGORY ALEX ALEXANDER EMMA OLIVIA AVA SOPHIA ISABELLA AMELIA EVELYN ABIGAIL EMILY ELLA'.split())
PEER=re.compile(r'\b(?:ZELLE|VENMO|CASH\s*APP|APPLE\s*CASH|P2P|PERSONAL\s+PAYMENT|FRIENDS?\s*(?:AND|&)\s*FAMILY|PAYPAL\s+PERSONAL)\b',re.I)
BANK=re.compile(r'\b(?:BANK\s+TRANSFER|ONLINE\s+TRANSFER|INTERNAL\s+TRANSFER|TRANSFER\s+(?:TO|FROM)|ACH\s+TRANSFER|CASH\s*OUT|ADD\s+MONEY)\b',re.I)
OTHER_RAIL=re.compile(r'\b(?:PAYPAL|CHECK|CHEQUE|ATM|WIRE|WITHDRAWAL|CASH\s+ADVANCE|MONEY\s+ORDER)\b',re.I)


def rail_kind(raw):
 if not isinstance(raw,str):return None
 if re.search(r'\bVENMO\b',raw,re.I) and re.search(r'\b(?:CASH\s*OUT|ADD\s+MONEY|BANK\s+TRANSFER)\b',raw,re.I):return 'bank_transfer'
 if PEER.search(raw):return 'peer_payment'
 if BANK.search(raw):return 'bank_transfer'
 if OTHER_RAIL.search(raw):return 'excluded'
 return None


def person_like(raw):
 if not isinstance(raw,str):return True
 words=re.findall(r'[A-Za-z]+',raw.upper())
 if re.search(r'\b(?:MR|MRS|MS|MISS|DR)\.?\s+[A-Z]',raw,re.I) or '@' in raw:return True
 return any(a in FIRST_NAMES and b not in {'INC','LLC','LTD','CORP','CO'} for a,b in zip(words,words[1:]))


def collapse_fragments(value):
 # Collapse whole repeated strings plus a trailing prefix, without substring guessing.
 for size in range(3,len(value)//2+1):
  unit=value[:size]
  if value.casefold()==(unit*((len(value)+size-1)//size))[:len(value)].casefold():return unit.strip()
 words=value.split()
 for size in range(1,len(words)//2+1):
  if len(words)%size==0 and words==words[:size]*(len(words)//size):return ' '.join(words[:size])
 return value


def clean_business_label(value,location=None):
 if not isinstance(value,str) or not value.strip() or len(value)>500:return None
 if any(unicodedata.category(c).startswith('C') for c in value):return None
 if person_like(value) or rail_kind(value):return None
 text=' '.join(unicodedata.normalize('NFKC',value).split())
 if location:
  city=location.get('city') or '';region=location.get('region') or ''
  if city and text.casefold() in {city.casefold(),(city+' '+region).strip().casefold()}:return None
  if city:text=re.sub(r'\s+'+re.escape(city)+r'(?:\s+'+re.escape(region)+r')?\s*$','',text,flags=re.I)
 text=re.sub(r'https?://\S+|www\.\S+|\b[a-z0-9.-]+\.(?:com|net|org|io)(?:/\S*)?',' ',text,flags=re.I)
 text=re.sub(r'\b(?:AUTH|REF(?:ERENCE)?|TRACE|STORE|TERM(?:INAL)?|SEQ|CARD)\s*[#*: -]*[A-Z0-9-]*\d[A-Z0-9-]*\b',' ',text,flags=re.I)
 text=re.sub(r'\+?\d[\d() .-]{7,}\d',' ',text)
 text=re.sub(r'\b\d+(?:[/.-]\d+)*\b',' ',text)
 text=' '.join(text.strip(' *#-/').split())
 text=collapse_fragments(text)
 if not text or len(text)>120 or re.search(r'\d',text):return None
 return text


def descriptor_label(transaction):
 # Both fields stay private. Reject conflicting brands or any personal/rail evidence.
 values=[value for value in (transaction.get('original_description'),transaction.get('name')) if value]
 if any(person_like(value) or rail_kind(value) for value in values):return None
 labels={label for value in values if (label:=single_descriptor_label(value,transaction.get('location')))}
 return next(iter(labels)) if len(labels)==1 else None


def single_descriptor_label(raw,location=None):
 if person_like(raw) or rail_kind(raw):return None
 # Keep a public billing-domain brand token, but discard all URL paths/query text.
 for key in BRANDS:
  raw=re.sub(r'(?:https?://)?(?:www\.)?\b'+key+r'\.COM(?:/[^\s]*)?',key,raw,flags=re.I)
 for host,replacement in (('CLAUDE.AI','ANTHROPIC CLAUDE'),('CHATGPT.COM','OPENAI CHATGPT')):
  raw=re.sub(r'(?:https?://)?(?:www\.)?'+re.escape(host)+r'(?:/[^\s]*)?',replacement,raw,flags=re.I)
 text=clean_business_label(raw,location)
 if not text:return None
 text=re.sub(r'\b(?:CHECKCARD|DEBIT|CREDIT|CARD|PURCHASE|RECURRING|POS|VISA|MASTERCARD)\b',' ',text,flags=re.I)
 # Only generic well-known location suffixes, or structured provider locations above.
 text=re.sub(r'\s+(?:SAN FRANCISCO CA|NEW YORK NY|SEATTLE WA|BOSTON MA|SAN JOSE CA|LOS ANGELES CA|REDMOND WA|US|USA)\s*$','',text,flags=re.I)
 tokens=re.findall(r'[A-Z]+',text.upper())
 for key,(label,products) in BRANDS.items():
  if key in tokens and set(tokens)<={key,*products,'COM','SUBSCRIPTION','SUBSCR','SUB','API','CREDITS','USAGE','PAYMENT','BILLING'}:return label
 return None
