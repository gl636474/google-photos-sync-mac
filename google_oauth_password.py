from oauthlib.oauth2 import LegacyApplicationClient
from requests_oauthlib import OAuth2Session

client_id = '1021661992594-ir7k24li0c6l9595nrc14aqh1dr8aphp.apps.googleusercontent.com'
client_secret = input('client_secret: ')
auth_uri = 'https://accounts.google.com/o/oauth2/auth'
token_uri = 'https://oauth2.googleapis.com/token'
scopes = ['https://www.googleapis.com/auth/photoslibrary.readonly']

username = input("Google username: ")
password = input("Google password: ")

client = LegacyApplicationClient(client_id=client_id)
session = OAuth2Session(client=client, scope=scopes)

token = session.fetch_token(token_url=token_uri,
                          username=username,
                          password=password, 
                          client_id=client_id,
                          client_secret=client_secret)

# Google returns unsupported grant type: password

print(token)
