from requests_oauthlib import OAuth2Session

client_id = '1021661992594-ir7k24li0c6l9595nrc14aqh1dr8aphp.apps.googleusercontent.com'
client_secret = input('client_secret: ')
auth_uri = 'https://accounts.google.com/o/oauth2/auth'
token_uri = 'https://oauth2.googleapis.com/token'
scopes = ['https://www.googleapis.com/auth/photoslibrary.readonly']
redirect_uris = ["urn:ietf:wg:oauth:2.0:oob","http://localhost"]
extra = {'client_id': client_id,
         'client_secret': client_secret}
authorization_base_url = "https://accounts.google.com/o/oauth2/v2/auth"

session = OAuth2Session(client_id, scope=scopes,
                        redirect_uri=redirect_uris[0])
#                        auto_refresh_url=token_uri,
#                        auto_refresh_kwargs=extra,
#                        token_updater=save_token)

            # Redirect user to Google for authorization
            
authorization_url, state = session.authorization_url(
                authorization_base_url,
                access_type="offline",
                prompt="select_account")
print('Please go here and authorize,', authorization_url)

# Get the authorization verifier code from the callback url
response_code = input('Paste the response token here:')

# Fetch the access token
token = session.fetch_token(
                token_uri, client_secret=client_secret,
                code=response_code)


print(token)