from oauthlib.oauth2 import BackendApplicationClient
from requests_oauthlib import OAuth2Session

client_id = '1021661992594-ir7k24li0c6l9595nrc14aqh1dr8aphp.apps.googleusercontent.com'
client_secret = 'u-FX-g4kNTKxHErjD73-St4t'
auth_uri = 'https://accounts.google.com/o/oauth2/auth'
token_uri = 'https://oauth2.googleapis.com/token'
scope = ['https://www.googleapis.com/auth/photoslibrary.readonly']

client = BackendApplicationClient(client_id=client_id, scope=scope)
session = OAuth2Session(client=client)
token = session.fetch_token(token_url=token_uri, client_id=client_id, client_secret=client_secret)

# Client Credentials Flow not supported (no user specified)

print(token)
