from oauthlib.oauth2 import MobileApplicationClient
from requests_oauthlib import OAuth2Session

client_id = '1021661992594-ir7k24li0c6l9595nrc14aqh1dr8aphp.apps.googleusercontent.com'
client_secret = 'u-FX-g4kNTKxHErjD73-St4t'
auth_uri = 'https://accounts.google.com/o/oauth2/auth'
token_uri = 'https://oauth2.googleapis.com/token'
scopes = ['https://www.googleapis.com/auth/photoslibrary.readonly']

client = MobileApplicationClient(client_id=client_id)
session = OAuth2Session(client=client, scope=scopes)

authorization_url, state = session.authorization_url(auth_uri)
print(authorization_url)

response = session.get(authorization_url)
token = session.token_from_fragment(response.url)

# Google complains of missingauth uri so doesn't repspond with
# a token
print(token)
