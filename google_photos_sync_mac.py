from pathlib import Path
from requests.adapters import HTTPAdapter
from requests_oauthlib import OAuth2Session
from urllib3.util.retry import Retry
import json
import os

## ############################################################################
## Global config
## ############################################################################

credentials_file = Path('./credentials.json')
saved_token_file = Path('./access_token.json')
mac_photos_dir = Path('/Users/gareth/Pictures/Photos Library.photoslibrary/Masters')
max_retries_per_request = 3
media_items_list_page_size = 25
authorization_base_url = "https://accounts.google.com/o/oauth2/v2/auth"
scopes = ['https://www.googleapis.com/auth/photoslibrary.readonly']

# A cache of the contents of saved_token_file. This is "global'd" by load_token
# and save_token so that if the token is auto-refreshed by requests_oauthlib
# then the refreshed value is immediately available to all
token = None

## ############################################################################
## Helper methods
## ############################################################################

def load_token():
    """Load a previously saved access token from filesystem"""
    global token
    
    try:
        with saved_token_file.open('r') as token_stream:
            token = json.load(token_stream)
    except (json.JSONDecodeError):
        print('Ignoring badly formatted JSON token file:', saved_token_file)
        return None
    except (FileNotFoundError):
        print('No saved token file:', saved_token_file)
        return None
    except (IOError):
        print('Ignoring inaccessible token file:', saved_token_file)
        return None
    return token

def save_token(new_token):
    """Persist an access token to the filesystem"""
    global token, saved_token_file
    
    token = new_token
    with saved_token_file.open('w') as token_stream:
        json.dump(new_token, token_stream)
    saved_token_file.chmod(0o600)

def request_new_token(client_id,
                      client_secret,
                      scopes,
                      redirect_uri,
                      token_uri, 
                      extra, 
                      authorization_base_url):
    """Get a completely fresh access token and save it to filesystem"""  
    
    session = OAuth2Session(client_id, scope=scopes,
                            redirect_uri=redirect_uri,
                            auto_refresh_url=token_uri,
                            auto_refresh_kwargs=extra,
                            token_updater=save_token)

    # Direct user to Google for authorization
    authorization_url, _ = session.authorization_url(
        authorization_base_url,
        access_type="offline",
        prompt="select_account")
    print('Please paste this link into your browser to authorize this app to access your Google Photos:')
    print(authorization_url)

    # Get the authorization verifier code from Google
    response_code = input('Paste Google\'s response code here: ')

    # Fetch the access token
    token = session.fetch_token(token_uri,
                                client_secret=client_secret,
                                code=response_code)
    save_token(token)

    return session

def parse_get_mediaitems_response(response, photos):
    """Parses the response object from a Google API GET mediatItems request, adding
    filename: metadata entries to photos argument and returns the nect page token,
    if any."""
    response_content = json.loads(response.content)
    if 'nextPageToken' in response_content:
        next_page_token = response_content['nextPageToken']
    else:
        next_page_token = None
        
    if 'mediaItems' in response_content:
        media_items = response_content['mediaItems']
        
        for media_item_meta_data in media_items:
            if 'filename' in media_item_meta_data:
                file_name = media_item_meta_data['filename']
                photos[file_name] = media_item_meta_data
            else:
                print('Missing filename property in mediaItem - skipping item')
    else:
        print('Missing mediaItems property in response - skipping page')

    return next_page_token

## ############################################################################
## Main Routine
## ############################################################################

try:
    # Create dict of info contained in credentials file
    with credentials_file.open('r') as stream:
        all_credentials = json.load(stream)
        
    # Everyting is under the "installed" root level entry
    installed_credentials = all_credentials['installed']
    
    # Save the info we need
    client_id = installed_credentials['client_id']
    client_secret = installed_credentials['client_secret']
    redirect_uri = installed_credentials['redirect_uris'][0]
    token_uri = installed_credentials['token_uri']
    
    # Copied from readthedocs.io examples. Not sure why this has to be
    extra = {
        'client_id': client_id,
        'client_secret': client_secret,
    }
    
except (json.JSONDecodeError, IOError):
    print('Missing or invalid JSON file: {}'.format(credentials_file))
    exit(1)
    
# Create a session (with auto-retries and auto-token-refresh) either from an 
# existing fresh or stale token or from scratch
token = load_token()
if token:
    # Silently reuse existing token - oauthlib handles refreshing stale tokens
    session = OAuth2Session(client_id,
                            token=token,
                            auto_refresh_url=token_uri,
                            auto_refresh_kwargs=extra,
                            token_updater=save_token)
else:
    # Need fresh token - will require user to authenticate with Google
    session = request_new_token(client_id, client_secret, scopes, redirect_uri,
                                token_uri, extra, authorization_base_url)

retries = Retry(total=max_retries_per_request,
                backoff_factor=0.1,
                status_forcelist=[500, 502, 503, 504],
                method_whitelist=frozenset(['GET']),
                raise_on_status=False)
session.mount('https://', HTTPAdapter(max_retries=retries))

# Google mediaItems GET wants Content-type header and pageSize param on every call
# Authorization header will change on token refresh so must be added for each request
session.headers.update({'Content-type': 'application/json'})
session.params.update({'pageSize': media_items_list_page_size})

# Dict of filename: metadata_dict
photos = dict()

# Get the first batch of photos-metadata and populate photos dict
response = session.get('https://photoslibrary.googleapis.com/v1/mediaItems',
                       headers={'Authorization': 'Bearer '+token['access_token']})
next_page_token = parse_get_mediaitems_response(response, photos)

# TODO uncomment:

# Repeat whilst Google returns a token indicating more items to come
#while next_page_token != None:
#    print('Got',len(photos),'. Fetching next page with token',next_page_token,'...')
#    response = session.get('https://photoslibrary.googleapis.com/v1/mediaItems',
#                       headers={'Authorization': 'Bearer '+token['access_token']},
#                       params={'pageToken': next_page_token})
#    next_page_token = parse_get_mediaitems_response(response, photos)
    
print (len(photos),'photos found in Google Photos online')
    
# Inspect filesystem for photo files
photo_files_on_disk = dict()
for (dirpath, dirnames, filenames) in os.walk(mac_photos_dir):
    for filename in filenames:
        photo_files_on_disk[filename] = os.path.join(dirpath, filename)
print (len(photo_files_on_disk),'files found in Photos library on disk')

# Compare filenames from Google and filesystem
photos_to_download = dict()
for filename in photos:
    if filename not in photo_files_on_disk:
        photos_to_download[filename] = photos[filename]
print(len(photos_to_download),'photos need to be downloaded from Google')

