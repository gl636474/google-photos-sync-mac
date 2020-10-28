import applescript
import time
from pathlib import Path

list_script = """
tell application "Photos"
    set mediaItems to every media item
    repeat with mediaItem in mediaItems
        set mediaItemFileName to filename of mediaItem
        log (mediaItemFileName as string)
    end repeat
end tell
"""

print("Starting")
start_time = time.time()
list_process = applescript.run(list_script)
while list_process.running:
    print("waiting...")
    time.sleep(2)
end_time = time.time()

total_time = end_time - start_time
line_count = list_process.text.count('\n')

print("took {} seconds, contains {} lines".format(total_time, line_count))


with Path('./photos_list_out.txt').open('w') as stream:
    stream.write(list_process.text)
    