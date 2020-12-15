
__version__ = "0.9"

from pathlib import Path
from requests.adapters import HTTPAdapter
from requests_oauthlib import OAuth2Session
from urllib3.util.retry import Retry
from subprocess import Popen, TimeoutExpired, PIPE
import applescript
import argparse
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from time import strptime, mktime, sleep, time

## ############################################################################
## Default config - can be overridden by command line arguments
## ############################################################################
default_cache_dir = Path.home() / '.google-photos-sync-mac'
default_credentials_file_name = 'credentials.json'
default_max_retries_per_request = 3
default_mac_photos_dir = Path.home() / 'Pictures' / 'Photos Library.photoslibrary'
default_fetch_size = 50

## ############################################################################
## Global config
## ############################################################################
import_applescript_file_name = 'import_photos.applescript'
list_applescript_file_name = 'list_photos.applescript'
users_cache_dir_name = 'users'
users_photos_dir_name = 'photos'
process_wait_sleep_time = 5 # seconds
process_wait_completion_time = 600 # seconds

authorization_base_url = "https://accounts.google.com/o/oauth2/v2/auth"
scopes = ['https://www.googleapis.com/auth/photoslibrary.readonly']

## ############################################################################
## Main Routine
## ############################################################################

def main():
    """Performs the following steps:
       * Find photo files in the specified Photos library
       * For each cached user:
          * Refresh existing access token or direct user to Google page for
            authorisation and store the new access token
          * Get list of all photo filenames from Google
          * Determine missing photos
          * Download the missing photos
          * Implort into Photos library
       * Tidy up cache dirs and wait for any still running sub-processes"""
    args = parse_arguments()
    
    if args.verbose:
        print('Inspecting photos library: {}'.format(args.mac_photos_library), flush=True)

    # dict of filename: full_file_path
    photo_files_on_disk = list_library_photos(args.mac_photos_library, args.verbose, args.case_sensitive)
    
    if photo_files_on_disk == None or len(photo_files_on_disk) == 0:
        error_print("Could not get list of photo filenames from MasOS Photos app")
    
    for nickname in get_users(args):
        
        if args.verbose:
            print('Processing user {}'.format(nickname), flush=True)
        
        user_cache_dir = get_user_cache_dir(args, nickname)
        user_photos_dir = user_cache_dir / users_photos_dir_name
        
        # empty the user's photos cache dir
        if user_photos_dir.exists():
            shutil.rmtree(user_photos_dir)
        user_photos_dir.mkdir()
    
        token_persister = TokenPersister(user_cache_dir)
        session = create_session(nickname, args, token_persister)

        if session == None:
            if args.verbose:
                print("Skipping user {} - no Google acces token. Run interactively (without --batch-mode)."
                      .format(nickname), flush=True)
            continue
        
        # Dict of filename: photo_metadata_dict
        photos = dict()
        
        # The token should exist now
        token = token_persister.load_token()
        
        # Get the first batch of photos-metadata and populate photos dict
        if args.verbose:
            print("Fetching list of photos from Google...", flush=True)
        response = session.get('https://photoslibrary.googleapis.com/v1/mediaItems',
                               headers={'Authorization': 'Bearer '+token['access_token']})
        next_page_token = parse_get_mediaitems_response(response, photos)
        
        # Repeat whilst Google returns a token indicating more items to come
        while next_page_token != None:
            if args.verbose >= 3:
                print('Got {} photos. Fetching next page with token "..{}".'
                      .format(len(photos), next_page_token[-27:]), flush=True)
            elif args.verbose >= 2:
                print('Got {} photos.'.format(len(photos)), flush=True)
                
            response = session.get('https://photoslibrary.googleapis.com/v1/mediaItems',
                               headers={'Authorization': 'Bearer '+token['access_token']},
                               params={'pageToken': next_page_token})
            next_page_token = parse_get_mediaitems_response(response, photos)
        if args.verbose:
            print (len(photos),'photos found in Google Photos online')
            
        # Compare filenames from Google and filesystem.
        # Create dict of filename: photo_metadata_dict
        photos_to_download = dict()
        for filename in photos:
            if args.case_sensitive:
                need_to_download = filename not in photo_files_on_disk
            else:
                need_to_download = filename.lower() not in photo_files_on_disk
                
            if need_to_download:
                photos_to_download[filename] = photos[filename]
            
            if args.max_downloads > 0:
                # We have a maximum number allowed to download
                if len(photos_to_download) >= args.max_downloads:
                    break
        
        if args.verbose:
            print(len(photos_to_download),'photos need to be downloaded from Google', flush=True)
        
        if not args.dry_run:
            # Download each photo from Google
            num_successful_downloads = 0
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
                
                if download_file(session, url, filename, user_photos_dir, file_creation_date, args.verbose):
                    num_successful_downloads += 1
            
            if args.verbose:
                print("{} of {} photos successfully downloaded".format(num_successful_downloads, len(photos_to_download)), flush=True)
        
            # Import photos for this user
            if num_successful_downloads > 0:
                if args.verbose:
                    print('Importing photos for user {}'.format(nickname), flush=True)
                    
                user_cache_dir = get_user_cache_dir(args, nickname)
                user_photos_dir = user_cache_dir / users_photos_dir_name
    
                import_photos(user_photos_dir, args.mac_photos_library, args.cache_dir, args.verbose)
    
            else:
                if args.verbose:
                    print('Skiping import for user {} - no photos to import'.format(nickname), flush=True)

        else:
            # Dry run - just print out files to download
            for filename, photo_metadata in photos_to_download.items():
                mime_type = photo_metadata['mimeType']
                print('   {} ({})'.format(filename, mime_type), flush=True)
    
    # End of looping through users to download / import
    
    if not args.dry_run:
        # Loop through each user deleting photos
    
        if len(photos_to_download) > 0:
            if not args.keep_downloads:
                if args.verbose:
                    print("Deleting downloaded and imported photos...", flush=True)
                for nickname in get_users(args):
                    user_cache_dir = get_user_cache_dir(args, nickname)
                    user_photos_dir = user_cache_dir / users_photos_dir_name
                    shutil.rmtree(user_photos_dir)
            else:
                if args.verbose:
                    print("Downloaded photos have been kept in:", flush=True)
                    for nickname in get_users(args):
                        user_cache_dir = get_user_cache_dir(args, nickname)
                        user_photos_dir = user_cache_dir / users_photos_dir_name
                        print("   {}".format(user_photos_dir), flush=True)

    if args.verbose:
        print("Done", flush=True)

## ############################################################################
## Helper classes
## ############################################################################

class TokenPersister:
    """Saves and loads tokens to/from the filesystem. Handles user-specific
    cache directories. This class defines a __call__() so that an instance can
    be supplied instead of a method expecting a single argument."""

    
    def __init__(self, user_cache_dir, token_file_name='access_token.json'):
        """Creates a new token persister which will save tokens to the given
        directory."""
        self._token_file_path = Path(user_cache_dir) / token_file_name
        self._token = None
    
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
                print('Ignoring badly formatted JSON token file:', self._token_file_path, flush=True)
                return None
            except (FileNotFoundError):
                print('No saved token file:', self._token_file_path, flush=True)
                return None
            except (IOError):
                print('Ignoring inaccessible token file:', self._token_file_path, flush=True)
                return None
        return self._token

## ############################################################################
## Helper methods
## ############################################################################

def error_print(message, code=1):
    """Prints the message to stderr and exits with the specified code."""
    print(message, file=sys.stderr, flush=True)
    exit(code)

def request_new_token(nickname,
                      client_id,
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
    print("Please paste this link into your browser to authorize this app to access {}'s Google Photos:"
          .format(nickname), flush=True)
    print(authorization_url, flush=True)

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
                print('Missing filename property in mediaItem - skipping item', flush=True)
    else:
        print('Missing mediaItems property in response - skipping page', flush=True)

    return next_page_token

def create_session(nickname, args, token_persister):
    """Create and returns a requests.Session object (with auto-retries
    and auto-token-refresh) either from an existing fresh or stale access token,
    or from scratch (i.e. asking user to authenticate with Google)."""
    
    user_token = token_persister.load_token()
    
    if user_token:
        # Silently reuse existing token - oauthlib handles refreshing stale tokens
        session = OAuth2Session(args.client_id,
                                token=user_token,
                                auto_refresh_url=args.token_uri,
                                auto_refresh_kwargs=args.extra,
                                token_updater=token_persister)
    elif not args.batch_mode:
        # Need fresh token - will require user to authenticate with Google
        session = request_new_token(nickname, args.client_id,
                                    args.client_secret, scopes,
                                    args.redirect_uri, args.token_uri,
                                    args.extra, authorization_base_url,
                                    token_persister)
    else:
        # No token at all but we cannot interactively ask for authentication
        return None
    
    # Either way, configure the session
    retries = Retry(total=args.max_retries,
                    backoff_factor=0.1,
                    status_forcelist=[500, 502, 503, 504],
                    method_whitelist=frozenset(['GET']),
                    raise_on_status=False)
    session.mount('https://', HTTPAdapter(max_retries=retries))
    
    # GET mediaItems needs Content-type header and pageSize param on every call
    session.headers.update({'Content-type': 'application/json'})
    session.params.update({'pageSize': args.fetch_size})

    # Authorization header will change on token refresh so it must be added
    # separately on each request
    
    return session

def list_library_photos_sqlite(photos_library, verbose=False, case_sensitive=False):
    """Returns a dict of all the photo-file-names in the MacOS Photos library by
    inspecting the Photos.sqlite database file. Returns None if the file does
    not exist or is of unexpected format (i.e. difference version of Photos).
    The key is the photo filename and the value just True."""

    photos_sqlite_db_path = photos_library / 'database' / 'Photos.sqlite'
    if photos_sqlite_db_path.is_file():
        
        if verbose >=2:
            print("{} is an SQLLite based library".format(photos_library), flush=True)
        
        photos = dict()
        if verbose >=2:
            print("Opening SQLLite db {}".format(photos_sqlite_db_path))
        with sqlite3.connect(photos_sqlite_db_path) as db_conn:
            db_curs = db_conn.cursor()
            try:
                for row in db_curs.execute("""select ZORIGINALFILENAME from ZADDITIONALASSETATTRIBUTES"""):
                    if case_sensitive:
                        original_filename = row[0]
                    else:
                        original_filename = str(row[0]).lower()
                    photos[original_filename] = True
            except:
                return None
        return photos
    else:
        # Not an sqlite-based version of Photos
        return None
    
def list_library_photos_filesystem(photos_library, verbose=False, case_sensitive=False):
    """Returns a dict of all the photo-file-names in the MacOS Photos library by
    inspecting the filesystem. The key is the photo filename, the value the full
    path to the file. Returns None if the file structure on disk is not as
    expected (i.e. not a version or configuration of Photos that stores photos
    as files on disk)."""
    
    photos_masters_dir_path = photos_library / 'Masters'
    if photos_masters_dir_path.is_dir():
        if any(entry.is_dir() for entry in photos_masters_dir_path.iterdir()):

            if verbose:
                print("{} is a filesystem based library".format(photos_library), flush=True)

            photo_files_on_disk = dict()
            for (dirpath, _, filenames) in os.walk(photos_masters_dir_path):
                for filename in filenames:
                    if case_sensitive:
                        key = filename
                    else:
                        key = filename.lower()
                    photo_files_on_disk[key] = os.path.join(dirpath, filename)
            
            return photo_files_on_disk
    
    # Not a fielsystem configured version of Photos
    return None

def list_library_photos_applescript(photos_library, verbose=False, case_sensitive=False):
    """Returns a dict of all the photo-file-names in the MacOS Photos library by
    querying Photos using applescript. This should work for all versions of
    Photos, however it can be slow (up to 5 mins for a library of 20,000 media
    items. The key is the photo filename and the value just True."""

    list_library_alias = create_macos_alias(photos_library)
    if verbose:
        print("Using AppleScript to query library {}".format(list_library_alias), flush=True)

    list_script = """-- generated file - do not edit
    
tell application "Photos"
    activate
    delay 2
    open "{}"
    delay 2
    set mediaItems to every media item
    repeat with mediaItem in mediaItems
        set mediaItemFileName to filename of mediaItem
        log (mediaItemFileName as string)
    end repeat
end tell
""".format(list_library_alias)

    list_process = applescript.run(list_script, background=True)
    while list_process.running:
        if verbose:
            print("Waiting for Photos to list all media items...", flush=True)
        sleep(process_wait_sleep_time)
    
    photos = dict()
    for line in list_process.text.splitlines():
        if case_sensitive:
            photos[line] = True
        else:
            photos[line.lower()] = True

    return photos

def list_library_photos(photos_library, verbose=False, case_sensitive=False):
    """Returns a dict of all the photo-file-names in the MacOS Photos library or
    None. The key will always be the photo filename. Depending upon the library
    version, the value may be the full filepath or just True. This method will
    try several techniques for obtaining the list of photos (fastest first). If
    all fail, None is returned."""
    
    photos_library = Path(photos_library)
    
    photos = list_library_photos_sqlite(photos_library, verbose, case_sensitive)
    
    if photos == None:
        photos = list_library_photos_filesystem(photos_library, verbose, case_sensitive)
    
    if photos == None:
        photos = list_library_photos_applescript(photos_library, verbose, case_sensitive)
  
    if not photos == None:
        if verbose:
            print("Found {} photos in MacOS Photos library".format(len(photos)), flush=True)
        if verbose >= 3:
            for photo_file in photos:
                print('   {}'.format(photo_file), flush=True)

    return photos
    
def create_macos_alias(path):
    """Returns a string that is an AppleScript alias representing the supplied path."""
    alias = Path(path).resolve()
    alias = "Macintosh HD" + str(alias)
    alias = alias.replace('/', ':')
    return alias
    
def import_photos(photos_directory_to_import, photos_library, temp_cache_dir=tempfile.gettempdir(), verbose=0):
    """Imports the photos in photos_dirctory_to_import into the specified MacOS
    Photos library. This method needs a temporary directory in which to store
    and run an applescript file. This tempory directory can be explicitly
    specified by temp_cache_dir, if not supplied the system default temp
    directory will be used.""" 
    
    temp_cache_dir = Path(temp_cache_dir).resolve()
    import_library_alias = create_macos_alias(photos_library)
    import_folder_alias = create_macos_alias(photos_directory_to_import)
    
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
    open "{}"
    delay 2
    import imageList
end tell
    
""".format(import_folder_alias, import_library_alias)

    applescript_file_path = temp_cache_dir / import_applescript_file_name
    with applescript_file_path.open('w') as stream:
        stream.write(import_applescript)
    
    with Popen(["osascript", applescript_file_path], stdin=sys.stdin, stdout=PIPE, stderr=PIPE, bufsize=1, universal_newlines=True) as p:
        start_time = time()
        end_time = start_time + process_wait_completion_time
        print("will wait {} seconds, untill {} for import sub-process".format(process_wait_completion_time, end_time), flush=True)
        while p.poll() is None and time() < end_time:
            try:
                if verbose >= 2:
                    print("Communitating with sub-process", flush=True)
                (import_stdout, import_stderr) = p.communicate(timeout=process_wait_sleep_time)
                if verbose >=1:
                    print("Import output>{}".format(import_stdout), flush=True)
                if import_stdout.strip():
                    print("Import error>{}".format(import_stderr), flush=True)
            except TimeoutExpired:
                if verbose >=2:
                    print("Timeout after {} seconds - retrying...".format(time - start_time), flush=True)

        if verbose >= 1:
            print("Import sub-process finished", flush=True)
    #return applescript.run(applescript_file_path, background=False)
   
    
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
    
    parser.add_argument('users', metavar='NICKNAME', nargs='*', help="""Names of
    users to synchronise. If none specified, all users will be synchronised. If
    NICKNAME has not previously been added via --add-user it will be ignored.
    See --list_users.""")
    
    parser.add_argument('-b', '--batch-mode', help="""Do not prompt user to
    authenticate with Google if there is no cached access token; in which case
    that user is ignored. Also do not prompt user if there are no Google client
    credentials cached or supplied on the command line; in which case the
    program will just terminate. Cannot be used with --add-user.""",
    action='store_true')
    
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
    
    parser.add_argument('-f', '--fetch-size', help="""When retrieving the list
    of photos from Google, retrieve in batches of this size. Defaults to {}"""
    .format(default_fetch_size), default=default_fetch_size, type=int)
    
    parser.add_argument('-m', '--max-downloads', help="""Maximum number of
    photos to downlaod from Google in this execution of this program. This is
    only useful to perform a quick test_parse_args run. Negative value means no limit (the
    default).""", type=int, default=-1)
    
    parser.add_argument('-x', '--max-retries', help="""Maximum number of retries
    for each individual GET request to Google. Defaults to {}."""
    .format(default_max_retries_per_request), type=int,
    default=default_max_retries_per_request)
    
    parser.add_argument('-v', '--verbose', help="""Output progress updates.
    Without this option only errors are outputted. Specify two or three times
    for even more verbose output.""", action='count')
    
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
    be passed to Google and Google usernames/passwords will never be stored
    or accessed by this program. The user will be directed to Google to enter
    username and password to suthenticate this program to access the user's
    photos. Adding an already existing user will have no effect. Cannot be used
    with --batch-mode.""", nargs='+', metavar='NICKNAME', dest='users_to_add')

    parser.add_argument('-z', '--remove-user', help="""Removes a google user
    account from the cached accounts to sync. NICKNAME is the same value that
    was passed to the -a/--add-user option. Removing and adding the same
    NICKNAME will clear stored credentials and previously downloaded photos for
    that user.""", nargs='+', metavar='NICKNAME',
    dest='users_to_remove')

    parser.add_argument('-y', '--dry-run', help="""Do not download or import
    photos but just print out the files which would have been downloaded and
    imported. Any --add-user and --remove-user will still be actionned.""",
    action='store_true')
    
    parser.add_argument('-n', '--case-sensitive', help="""Compare filenames
    using case sensitive string comparison, so "file.jpg" is considered a
    different filename to "file.JPG". Default is to ignore case.""",
    action='store_true')
    
    parser.add_argument('-u', '--list-users', help="""List the currently defiend
    users and exit""", action='store_true')

    # Help string auto generated. Auto exits after printing version string.
    parser.add_argument('--version', action='version',
                        version='%(prog)s {}'.format(__version__))

    args = parser.parse_args()
    
    if args.verbose == None:
        args.verbose = False
    
    if args.users_to_add != None and args.batch_mode:
        error_print("Cannot specify -a/--add-user and -b/--batch-mode")
    
    if not args.mac_photos_library.is_dir():
        error_print('{} is not a directory or Photos Library'.format(args.mac_photos_library))
    
    if not args.cache_dir.is_dir():
        args.cache_dir.mkdir(exist_ok=True)
    
    # If add/remove/list user(s) specified, do those commands and exit otherwise
    # continue to download/import
    exit_now = False
    
    # Remove cache-dirs for specified users
    if not args.users_to_remove == None:
        for nickname in args.users_to_remove:
            user_cache_dir = get_user_cache_dir(args, nickname)
            if user_cache_dir.is_dir():
                shutil.rmtree(user_cache_dir)
                if args.verbose:
                    print("Deleted user-cache directory for {}".format(nickname), flush=True)
            elif user_cache_dir.is_file():
                user_cache_dir.unlink()
                if args.verbose:
                    print("Deleted user-cache file(!?) for {}".format(nickname), flush=True)
        exit_now = True

    # Add empty cache-dirs for new users - will prompt later for Google
    # authentication
    if not args.users_to_add == None:
        for nickname in args.users_to_add:
            user_cache_dir = get_user_cache_dir(args, nickname)
            if not user_cache_dir.exists():
                user_cache_dir.mkdir(parents=True)
        exit_now = True
    
    if args.list_users:
        users = get_users(args)
        for user in users:
            print(user, flush=True)
        exit_now = True

    if exit_now:
        exit(0)
            
    # Read/Store credentials file and read info from it
    cached_credentials_file_path = args.cache_dir / default_credentials_file_name
    if args.credentials_file == None:
        # Use cached credentials file
        try:
            args.credentials_file = cached_credentials_file_path.open('r')
        except FileNotFoundError as e:
            error_print("No cached credentials file ({}) and no --credentials option specified"
                  .format(e.filename))
    else:
        # args.credentials_file is already an open file stream (see
        # parse_argumenrs())
        #
        # Using specified credentials file, cache it for subsequent use
        try:
            shutil.copyfile(args.credentials_file.name, cached_credentials_file_path)
        except shutil.SameFileError:
            error_print("Cancelled copying specified credentials file: same file")
        except IOError as e:
            error_print("Failed to copy credentials file {} to cache ({})\n{}"
                  .format(args.credentials_file.name, cached_credentials_file_path, e))

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
        error_print('Invalid JSON file: {}'.format(args.credentials_file.name))
    except KeyError as e:
        error_print("Missing JSON property '{}' in credentials file {} (and not supplied on command line"
                    .format(e, args.credentials_file.name))
    except IndexError as e:
        error_print("Missing (one or more) redirect URIs in credentials file {}"
                    .format(args.credentials_file.name))
    except IOError as e:
        error_print('Cannot read from file: {}\n{}'
                    .format(args.credentials_file.name, e))
    
    return args
    
def download_file(session, url, filename, directory, file_creation_timestamp=None, verbose=False):
    """Downloads a file from the specified URL to the specified destination
    directory and filename. Optionally sets the timestamp of the new file to the
    specified value which should be a string of the form "YYYY-MM-DDTHH:MM:SSZ".
    Verbose output (if specified) is sent to stdout."""

    # Download
    downloaded = False
    if verbose:
        print("Downloading {}...".format(filename), flush=True)
    response = session.get(url, stream=True)
    
    # Write to temp file, set dates, rename file to target filename
    temp_file = tempfile.NamedTemporaryFile(dir=directory, delete=False)
    temp_file_path = Path(temp_file.name)
    try:
        with temp_file:
            for chunk in response.iter_content(chunk_size=128):
                temp_file.write(chunk)
        
        if not file_creation_timestamp == None:
            try:
                file_creation_time_struct = strptime(file_creation_timestamp, '%Y-%m-%dT%H:%M:%SZ')
                file_creation_secs = int(mktime(file_creation_time_struct))
                os.utime(temp_file_path, (file_creation_secs, file_creation_secs))
            except (OSError, ValueError) as e:
                if verbose:
                    print("Error setting file date on {} ({})\n{}"
                          .format(temp_file_path, file_creation_timestamp, e), flush=True)
        temp_file_path.rename(directory / filename)
        downloaded = True
    except Exception as e:
        if verbose >= 2:
            print("Error downloading {}: {}".format(filename, e), flush=True)
    finally:
        temp_file.close()
        response.close()
    
    return downloaded

def get_user_cache_dir(args, nickname):
    """Returns the path to the cache directory for the given user."""
    return args.cache_dir / users_cache_dir_name / nickname

def get_users(args):
    """Returns a lisr of the nicknames of pre-cached users. Inspects the 
    users_cache_dir_name directory for subdirectories - each is a user cache."""
    users_directory = args.cache_dir / users_cache_dir_name
    users = []
    if args.users == None or len(args.users) == 0:
        # Return all users cached in users_cache_dir
        for child in users_directory.iterdir():
            if child.is_dir():
                users.append(child.name)
    else:
        # Return args.users who have a directory in users_cache_dir
        for user in args.users:
            user_dir = users_directory / user
            if user_dir.exists():
                users.append(user)
    
    return users
    
## ############################################################################
## Execution starts here
## ############################################################################

if __name__ == "__main__":
    main()        
        