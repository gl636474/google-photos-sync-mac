from pathlib import Path
from requests.adapters import HTTPAdapter
from requests_oauthlib import OAuth2Session
from urllib3.util.retry import Retry
import applescript
import argparse
import json
import os
import shutil
import sys
import tempfile
from time import strptime, mktime

## ############################################################################
## Default config - can be overridden by command line arguments
## ############################################################################
default_cache_dir = Path.home() / '.google-photos-sync-mac'
default_credentials_file_name = 'credentials.json'
default_max_retries_per_request = 3
default_mac_photos_dir = Path.home() / 'Pictures' / 'Photos Library.photoslibrary'
default_batch_size = 15

## ############################################################################
## Global config
## ############################################################################
import_applescript_file_name = 'import_photos.applescript'
users_cache_dir_name = 'users'
users_photos_dir_name = 'photos'

authorization_base_url = "https://accounts.google.com/o/oauth2/v2/auth"
scopes = ['https://www.googleapis.com/auth/photoslibrary.readonly']

## ############################################################################
## Helper classes
## ############################################################################

class TokenPersister:
    """Saves and loads tokens to/from the filesystem. Handles user-specific
    cache directories. This class defines a __call__() so that an instance can
    be supplied instead of a method expecting a single argument. The load and
    save methods of this class will also set the "token" global attribute. """
    
    _token_file_path = None
    _token = None
    
    def __init__(self, user_cache_dir, token_file_name='access_token.json'):
        """Creates a new token persister which will save tokens to the given
        directory."""
        self._token_file_path = Path(user_cache_dir) / token_file_name
    
    def __call__(self, new_token):
        """Persist the given token to the user-specific cache directory.
        This method just a pass-through to save_token so that an object
        of this class can be supplied in lieu of a single-argument method."""
        self.save_token(new_token)
    
    def save_token(self, new_token):
        """Persist the given token to the user-specific cache directory."""
        
        self._token = new_token
        with self._token_file_path.open('w') as token_stream:
            json.dump(new_token, token_stream)
        self._token_file_path.chmod(0o600)
    
    def load_token(self):
        """Load a previously saved access token from user-specific cache
        directory in the filesystem. Returns the previously saved token or
        None if no token was saved or the file is corrupt."""

        if self._token == None:
            try:
                with self._token_file_path.open('r') as token_stream:
                    self._token = json.load(token_stream)
            except (json.JSONDecodeError):
                print('Ignoring badly formatted JSON token file:', self._token_file_path)
                return None
            except (FileNotFoundError):
                print('No saved token file:', self._token_file_path)
                return None
            except (IOError):
                print('Ignoring inaccessible token file:', self._token_file_path)
                return None
        return self._token

## ############################################################################
## Helper methods
## ############################################################################

def request_new_token(client_id,
                      client_secret,
                      scopes,
                      redirect_uri,
                      token_uri, 
                      extra, 
                      authorization_base_url,
                      token_persister):
    """Get a completely fresh access token, save it to filesystem and return a
    new session. User will be asked to go to Google and grant permission."""  
    
    session = OAuth2Session(client_id, scope=scopes,
                            redirect_uri=redirect_uri,
                            auto_refresh_url=token_uri,
                            auto_refresh_kwargs=extra,
                            token_updater=token_persister)

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
    new_token = session.fetch_token(token_uri,
                                client_secret=client_secret,
                                code=response_code)
    token_persister.save_token(new_token)

    return session

def parse_get_mediaitems_response(response, photos):
    """Parses the response object from a Google API GET mediatItems request,
    adding filename: metadata entries to photos argument and returns the next
    page token, if any."""
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

def create_session(args, user_token=None, token_persister=None):
    """Create and returns a requests module session object (with auto-retries
    and auto-token-refresh) either from an existing fresh or stale access token,
    or from scratch (i.e. asking user to authenticate with Google) - if the
    user_token argument is None."""
    if user_token:
        # Silently reuse existing token - oauthlib handles refreshing stale tokens
        session = OAuth2Session(args.client_id,
                                token=user_token,
                                auto_refresh_url=args.token_uri,
                                auto_refresh_kwargs=args.extra,
                                token_updater=token_persister)
    else:
        # Need fresh token - will require user to authenticate with Google
        session = request_new_token(args.client_id, args.client_secret, scopes, 
                                    args.redirect_uri, args.token_uri, args.extra,
                                    authorization_base_url, token_persister)
    
    # Either way, configure the session
    retries = Retry(total=args.max_retries,
                    backoff_factor=0.1,
                    status_forcelist=[500, 502, 503, 504],
                    method_whitelist=frozenset(['GET']),
                    raise_on_status=False)
    session.mount('https://', HTTPAdapter(max_retries=retries))
    
    # GET mediaItems needs Content-type header and pageSize param on every call
    session.headers.update({'Content-type': 'application/json'})
    session.params.update({'pageSize': args.batch_size})

    # Authorization header will change on token refresh so it must be added
    # separately on each request
    
    return session
   
def import_photos(photos_directory_to_import, top_level_cache_dir):
    
    top_level_cache_dir = Path(top_level_cache_dir).resolve()
    photos_directory_to_import = Path(photos_directory_to_import).resolve()
    alias = "Macintosh HD"+photos_directory_to_import.name.replace('/', ':')
    
    # Double braces to escape
    import_applescript = """-- generated file - do not edit

set importFolder to alias "{}"

tell application "Finder" to set theFiles to every file of importFolder

set imageList to {{}}
repeat with i from 1 to number of items in theFiles
    set this_item to item i of theFiles as alias
    set the end of imageList to this_item
end repeat

tell application "Photos"
    activate
    delay 2
    import imageList
end tell
    
""".format(alias)

    applescript_file_path = top_level_cache_dir / import_applescript_file_name
    with applescript_file_path.open('w') as stream:
        stream.write(import_applescript)
    
    # TODO handle the return value - need to wait on it?
    applescript.run(applescript_file_path, background=False)
   
    
def parse_arguments():
    """parses any commandline arguments and returns an object with
    configuration settings"""
    
    parser = argparse.ArgumentParser(description="""Downloads and imports any
    new or missing photos from one or more Google Photos accounts into a MacOS
    Photos library.""", epilog="""Note that photos are compared by filename
    only. If multiple photos exist in a Google Photos acount with the same 
    filename, only the last one will be downloaded. If multiple Google Photos
    acounts are scanned, photos with the same filename as an already
    downloaded photo will be skipped.""")
    
    parser.add_argument('-d', '--cache-dir', help="""Use this directory to cache
    Google application credentials file, Google user authentication tokens
    and downloaded photos (before they are imported into the MacOS Photos
    library). Note that a new cache directory will require the --credentials
    option (or manually create the directory and place a credentials file
    in it) and also force re-authentication with Google. Directory is created
    if it does not already exist. Defaults to {}.""".format(default_cache_dir),
    metavar='DIRECTORY', default=default_cache_dir)
    
    # Raises error if file is specified and does not exist
    parser.add_argument('-c', '--credentials', help="""Use this application
    credentials JSON file instead of any pre-cached credentials. The specified
    file will then be cached overwriting any previously cached file. This
    argument must be specified the first time this rogram is run (unless a
    credentials file has been first manually placed in the cache directory
    (see --cache-dir)""", type=argparse.FileType('r'), metavar='FILE',
    dest='credentials_file')
    
    parser.add_argument('-i', '--client-id', help="""Use this string as the
    application client ID when authenticating with Google. Overrides any value
    specified in the cached credentials file or in the file specified with the
    --credentials option.""", metavar='STRING')
    
    parser.add_argument('-s', '--client-secret', help="""Use this string as the
    application client secret when authenticating with Google. Overrides any
    value specified in the cached credentials file or in the file specified
    with the --credentials option.""", metavar='STRING')
    
    parser.add_argument('-r', '--redirect-uri', help="""Use this string as the
    redirect URI when authenticating with Google. Overrides any
    value specified in the cached credentials file or in the file specified
    with the --credentials option.""", metavar='STRING')
    
    parser.add_argument('-t', '--token-uri', help="""Use this string as the
    token URI when authenticating with Google. Overrides any
    value specified in the cached credentials file or in the file specified
    with the --credentials option.""", metavar='STRING')
    
    parser.add_argument('-b', '--batch-size', help="""When retrieving the list
    of photos from Google, retrieve in batches of this size. Defaults to {}"""
    .format(default_batch_size), default=default_batch_size, type=int)
    
    parser.add_argument('-m', '--max-retries', help="""Maximum number of retries
    for each individual GET request to Google. Defaults to {}."""
    .format(default_max_retries_per_request), type=int,
    default=default_max_retries_per_request)
    
    parser.add_argument('-v', '--verbose', help="""Output progress updates.
    Without this option only errors are outputted.""", action='store_true')
    
    parser.add_argument('-l', '--mac-photos-library', help="""The Photos Library
    or top level directory to scan for existing photos. Defaults to {}."""
    .format(default_mac_photos_dir), default=default_mac_photos_dir, 
    type=Path)
    
    parser.add_argument('-k', '--keep-downloads', help="""Do not delete the
    photos downloaded from Google after importing into the MacOS Photos library.
    However, the photos will be deleted the next time this program is run.""",
    action='store_true')
    
    parser.add_argument('-a', '--add-user', help="""Add a google user account
    to sync. Note that the NICKNAME is **not** the Google username, it merely
    distinguishes multiple Google syncs on this machine. The NICKNAME will never
    be passed to Google and Google usernames/passwords will never be stored on
    or accessed by this program. The user will be directed to Google to enter
    username and password to suthenticate this program to access the users
    photos.""", nargs='+', metavar='NICKNAME', dest='users_to_add')

    parser.add_argument('-z', '--remove-user', help="""Removes a google user
    account from the cached accounts to sync. NICKNAME is the same value that
    was passed to the -a/--add-user option. Removing and adding the same
    NICKNAME will clear stored credentials and previously downloaded photos for
    that user.""", nargs='+', metavar='NICKNAME',
    dest='users_to_remove')

    args = parser.parse_args()
    
    if not args.mac_photos_library.is_dir():
        print('{} is not a directory or Photos Library'
              .format(args.mac_photos_library), file=sys.stderr)
        exit(1)
    
    if not args.cache_dir.is_dir():
        args.cache_dir.mkdir(exist_ok=True)
    
    # Remove cache-dirs for specified users
    if not args.users_to_remove == None:
        for nickname in args.users_to_remove:
            user_cache_dir = get_user_cache_dir(args, nickname)
            if user_cache_dir.is_dir():
                shutil.rmtree(user_cache_dir)
                if args.verbose:
                    print("Deleted user-cache directory for {}".format(nickname))
            elif user_cache_dir.is_file():
                user_cache_dir.unlink()
                if args.verbose:
                    print("Deleted user-cache file(!?) for {}".format(nickname))

    # Add empty cache-dirs for new users - will prompt later for Google
    # authentication
    if not args.users_to_add == None:
        for nickname in args.users:
            user_cache_dir = get_user_cache_dir(args, nickname)
            if not user_cache_dir.exists():
                user_cache_dir.mkdir(parents=True)
    
    # Read/Store credentials file and read info from it
    cached_credentials_file_path = args.cache_dir / default_credentials_file_name
    if args.credentials_file == None:
        # Use cached credentials file
        try:
            args.credentials_file = cached_credentials_file_path.open('r')
        except FileNotFoundError as e:
            print('Cannot open file {}'.format(e.filename), file=sys.stderr)
            exit(1)
    else:
        # Using specified credentials file, cache it for subsequent use
        try:
            shutil.copyfile(args.credentials_file.name, cached_credentials_file_path)
        except shutil.SameFileError:
            print("Cancelled copying specified credentials file: same file",
                  file=sys.stderr)
        except IOError as e:
            print("Failed to copy credentials file {} to cache ({})\n{}"
                  .format(args.credentials_file.name, cached_credentials_file_path, e))
            exit(1)

    try:
        # Create dict of info contained in credentials file
        with args.credentials_file as stream:
            all_credentials = json.load(stream)
            
        # Everyting is under the "installed" root level entry
        installed_credentials = all_credentials['installed']
        
        # Save the info we need
        if args.client_id == None:
            args.client_id = installed_credentials['client_id']
            
        if args.client_secret == None:
            args.client_secret = installed_credentials['client_secret']
            
        if args.redirect_uri == None:
            redirect_uris = installed_credentials['redirect_uris']
            if isinstance(redirect_uris, str):
                args.redirect_uri = redirect_uris
            else:
                # Assume list/tuple/sequence
                args.redirect_uri = redirect_uris[0]
        
        if args.token_uri == None:
            args.token_uri = installed_credentials['token_uri']
        
        # Copied from readthedocs.io examples. Not sure why this has to be
        args.extra = {
            'client_id': args.client_id,
            'client_secret': args.client_secret,
        }
        
    except json.JSONDecodeError:
        print('Invalid JSON file: {}'.format(args.credentials_file.name),
              file=sys.stderr)
        exit(1)
    except KeyError as e:
        print("Missing JSON property '{}' in credentials file {}"
              .format(e, args.credentials_file.name), file=sys.stderr)
        exit(1)
    except IndexError:
        print("Missing (one or more) redirect URIs in credentials file {}"
              .format(args.credentials_file.name), file=sys.stderr)
        exit(1)
    except IOError as e:
        print('Cannot read from file: {}\n{}'
              .format(args.credentials_file.name, e), file=sys.stderr)
        exit(1)
    
    return args
    
def download_file(url, filename, directory, file_creation_timestamp=None, verbose=False):
    """Downloads a file from the specified URL to the specified destination
    directory and filename. Optionally sets the timestamp of the new file to the
    specified value which should be a string of the form "YYYY-MM-DDTHH:MM:SSZ".
    Verbose output (if specified) is sent to stdout."""

    # Download
    if verbose:
        print("Downloading {}...".format(filename))
    response = session.get(url, stream=True)
    
    # Write to temp file, set dates, rename file to target filename
    temp_file = tempfile.NamedTemporaryFile(dir=directory, delete=False)
    temp_file_path = Path(temp_file.name)
    with temp_file:
        for chunk in response.iter_content(chunk_size=128):
            temp_file.write(chunk)
    temp_file.close()
    response.close()
    
    if not file_creation_timestamp == None:
        try:
            file_creation_timestamp = strptime(file_creation_timestamp, '%Y-%m-%dT%H:%M:%SZ')
            file_creation_timestamp = int(mktime(file_creation_timestamp))
            os.utime(temp_file_path, (file_creation_timestamp, file_creation_timestamp))
        except (OSError, ValueError) as e:
            if verbose:
                print("Error setting file date on {} ({})\n{}"
                      .format(temp_file_path, file_creation_date, e))
    temp_file_path.rename(directory / filename)

def get_user_cache_dir(args, nickname):
    """Returns the path to the cache directory for the given user."""
    return args.cache_dir / users_cache_dir_name / nickname

def get_users(args):
    """Returns a lisr of the nicknames of pre-cached users. Inspects the 
    users_cache_dir_name directory for subdirectories - each is a user cache."""
    users_directory = args.cache_dir / users_cache_dir_name
    users = []
    for child in users_directory.iterdir():
        if child.is_dir():
            users.append(child.name)
    return users
    
## ############################################################################
## Main Routine
## ############################################################################

args = parse_arguments()

# Inspect filesystem for photo files. Create dict of filename: full_file_path
photo_files_on_disk = dict()
for (dirpath, dirnames, filenames) in os.walk(args.mac_photos_library):
    for filename in filenames:
        photo_files_on_disk[filename] = os.path.join(dirpath, filename)
if args.verbose:
    print (len(photo_files_on_disk),'files found in Photos library on disk')


for nickname in get_users(args):
    
    user_cache_dir = get_user_cache_dir(args, nickname)
    user_photos_dir = user_cache_dir / users_photos_dir_name
    
    # empty the user's photos cache dir
    if user_photos_dir.exists():
        shutil.rmtree(user_photos_dir)
    user_photos_dir.mkdir()

    token_persister = TokenPersister(user_cache_dir)
    token = token_persister.load_token()
    session = create_session(args, token, token_persister)
    
    # Dict of filename: photo_metadata_dict
    photos = dict()
    
    # Get the first batch of photos-metadata and populate photos dict
    response = session.get('https://photoslibrary.googleapis.com/v1/mediaItems',
                           headers={'Authorization': 'Bearer '+token['access_token']})
    next_page_token = parse_get_mediaitems_response(response, photos)
    
    # TODO uncomment:
    
    # Repeat whilst Google returns a token indicating more items to come
    #while next_page_token != None:
    #    if args.verbose:
    #        print('Got',len(photos),'. Fetching next page with token',next_page_token,'...')
    #    response = session.get('https://photoslibrary.googleapis.com/v1/mediaItems',
    #                       headers={'Authorization': 'Bearer '+token['access_token']},
    #                       params={'pageToken': next_page_token})
    #    next_page_token = parse_get_mediaitems_response(response, photos)
    if args.verbose:
        print (len(photos),'photos found in Google Photos online')
        
    # Compare filenames from Google and filesystem.
    # Create dict of filename: photo_metadata_dict
    photos_to_download = dict()
    for filename in photos:
        if filename not in photo_files_on_disk:
            photos_to_download[filename] = photos[filename]
    if args.verbose:
        print(len(photos_to_download),'photos need to be downloaded from Google')
    
    # Download each photo from Google
    for filename, photo_metadata in photos_to_download.items():
        # Work out download url
        mime_type = photo_metadata['mimeType']
        if mime_type.startswith('image'):
            url_suffix = '=d'
        elif mime_type.startswith('video'):
            url_suffix = '=dv'
        else:
            if args.verbose:
                print("Skipping download of unknown media type {}: {}"
                      .format(mime_type, filename))
            continue
        url = photo_metadata['baseUrl']+url_suffix
        
        try:
            file_creation_date = photo_metadata['mediaMetadata']['creationTime']
        except:
            file_creation_date = None
        
        download_file(url, filename, user_photos_dir, file_creation_date, args.verbose)
    
    # Import to MacOS Photos
    import_photos(user_photos_dir, args.cache_dir)
    if not args.keep_downloads:
        shutil.rmtree(user_photos_dir)
        
        