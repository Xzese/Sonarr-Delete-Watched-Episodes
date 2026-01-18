# Plex/Jellyfin and Sonarr Episode Cleanup Script

This Python script automates the cleanup of watched episodes in your Plex or Jellyfin library by removing them from your Sonarr library and unmonitoring them in Sonarr. It reads configuration from environment variables and performs the cleanup based on the specified criteria.

## Prerequisites

Before using this script, make sure you have the following prerequisites installed:

- Python 3
- `plexapi` library: You can install it via pip (`pip install plexapi`)
- `jellyfin_apiclient_python` library: You can install it via pip (`pip install jellyfin_apiclient_python`)
- `pyarr` library: You can install it via pip (`pip install pyarr`)
- `python-dotenv` library: You can install it via pip (`pip install python-dotenv`)

## Setup

1. Clone or download this repository to your local machine.
2. Ensure you have the necessary environment variables set in a `.env` file:
   
   **Required for all setups:**
   - `SONARR_URL`: Your Sonarr server URL
   - `SONARR_KEY`: Your Sonarr API key (You can find it in Sonarr by navigating to Settings => General)
   - `DAYS_TO_DELETE`: Number of days until episodes are deleted (default: 2)
   - `DEFAULT_DELETE`: Delete episodes by default? Answer `true` or `false`. If not `false`, defaults to `true`
   - `LOG_FILE`: Path to the log file (default: output/log.txt)
   - `MEDIA_SERVICE`: Choose `plex` or `jellyfin` (default: plex)
   
   **For Plex:**
   - `PLEX_URL`: Your Plex server URL
   - `PLEX_TOKEN`: Your Plex authentication token (Refer to [Plex Support](https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/) for help finding)
   
   **For Jellyfin:**
   - `JELLYFIN_URL`: Your Jellyfin server URL
   - `JELLYFIN_TOKEN`: Your Jellyfin API token
   
   **Optional:**
   - `SHOULD_SLEEP`: Whether to sleep after execution (default: false)
   - `SLEEP_HOURS`: Number of hours to sleep if SHOULD_SLEEP is true (default: 24)

3. Run the script.

## Supported Media Services

### Plex
The script works with Plex media libraries. It filters watched episodes using the "Keep" and "Delete" genres.

### Jellyfin
The script also supports Jellyfin libraries. Episodes are identified and filtered based on their watch status and date.


## Usage

1. Run the script: `python delete_watched_episodes.py`
2. The script will:
   - Connect to your configured media service (Plex or Jellyfin)
   - Connect to Sonarr
   - Find watched episodes older than the specified number of days
   - Unmonitor and delete those episodes from Sonarr
   - Log all actions to the specified LOG_FILE
   - Optionally sleep for the specified duration if SHOULD_SLEEP is enabled

### Adding Shows to Delete or Keep Lists

#### For Plex:
You can use the "Keep" or "Delete" genres in Plex to control which shows are deleted:
- When `DEFAULT_DELETE` is set to `true`: Episodes with the "Keep" genre will be excluded from deletion
- When `DEFAULT_DELETE` is set to `false`: Only episodes with the "Delete" genre will be deleted

#### For Jellyfin:
Episodes are automatically identified as watched in Jellyfin and deleted based on their watch date and the `DAYS_TO_DELETE` setting. To prevent a series from being deleted, mark it as a favorite in Jellyfin and it will be skipped from the deletion logic.

## Note

- This script assumes you have both Sonarr and either Plex or Jellyfin set up and running with your media library managed by Sonarr.
- Ensure that you have set the correct permissions and configurations in both your media service and Sonarr before running this script.
- All deletions are logged to the specified LOG_FILE for your records.

## Logging

The script logs all actions to the file specified in the `LOG_FILE` environment variable. Each log entry includes a timestamp and a description of the action performed (episode deletion, season unmonitoring, etc.). If no episodes are found to delete, this is also logged.

## Scheduling

If you want to run this script automatically at regular intervals, you can set `SHOULD_SLEEP` to `true` and `SLEEP_HOURS` to your desired interval. The script will sleep for the specified number of hours after completing its tasks.
