import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import ray
from google.oauth2 import service_account
from googleapiclient.discovery import build
from ray.serve.handle import DeploymentHandle

logger = logging.getLogger(__name__)


@ray.remote(num_cpus=1)
class SchedulingActor:
    """
    Ray Actor that monitors a Google Calendar for scheduling model deployments.

    This actor periodically checks a Google Calendar for events that specify
    model deployment changes and communicates these to a SchedulingControllerDeployment.
    """

    def __init__(
        self,
        google_credentials_path: str,
        google_calendar_id: str,
        check_interval_s: float,
        controller_handle: DeploymentHandle,
    ):
        """
        Initialize the SchedulingActor.

        Args:
            google_credentials_path: Path to the Google API service account credentials file
            google_calendar_id: ID of the Google Calendar to monitor
            check_interval_s: Time in seconds between calendar checks
            controller_handle: Handle to the SchedulingControllerDeployment
        """
        self.google_credentials_path = google_credentials_path
        self.google_calendar_id = google_calendar_id
        self.check_interval_s = check_interval_s
        self.controller_handle = controller_handle

        # Store the hash of previous model keys to detect changes
        self.previous_model_keys_hash = hash("")

        # Google API authorization scopes
        scopes = ["https://www.googleapis.com/auth/calendar.readonly"]

        # Create credentials from the service account file
        credentials = service_account.Credentials.from_service_account_file(
            self.google_credentials_path, scopes=scopes
        )

        # Build the Google Calendar API service
        self.service = build("calendar", "v3", credentials=credentials)

    async def start(self):
        """
        Start the scheduling loop.

        This method continuously checks the calendar for changes at the
        specified interval.
        """

        print("Starting scheduler...")

        while True:

            try:
                self.check_calendar()
            except Exception as e:
                import traceback

                print(f"Error in check_calendar: {e}")
                traceback.print_exc()

            # Wait for the next check interval
            await asyncio.sleep(self.check_interval_s)

    def sanitize(self, description: str) -> str:
        """
        Sanitize event description by removing HTML tags and newlines.

        Args:
            description: Raw event description string

        Returns:
            str: Cleaned description string
        """
        description = description.replace("\n", "")
        CLEANR = re.compile("<.*?>")
        description = re.sub(CLEANR, "", description)
        return description

    def check_calendar(self):
        """
        Check the Google Calendar for events and process any changes.

        This method fetches the current and upcoming events from the calendar,
        detects changes, and informs the controller if needed.
        """

        # Calculate time boundaries for events (now to 1 second in the future)
        now = datetime.now(timezone.utc)
        future = now + timedelta(seconds=1)

        # Fetch events from the calendar
        events_result = (
            self.service.events()
            .list(
                calendarId=self.google_calendar_id,
                timeMin=now.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                timeMax=future.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                singleEvents=True,
                orderBy="startTime",
                timeZone="UTC",
            )
            .execute()
        )

        events = events_result.get("items", [])

        model_keys = sorted([self.sanitize(event["description"]) for event in events])

        # Generate a hash of the model keys to check for changes
        current_hash = hash("".join(model_keys))

        # Only update if the model keys have changed
        if current_hash != self.previous_model_keys_hash:
            print(
                "Change in model deployment state. Sending deployment request to Controller..."
            )
            # Update the stored hash
            self.previous_model_keys_hash = current_hash
            # Update the controller with new model keys
            self.controller_handle.deploy.remote(model_keys, dedicated=True)

    async def get_schedule(self):
        """
        Get scheduled events for the next week.

        Returns:
            Dict[str, Dict]: A dictionary mapping model_keys to their schedule information
                           containing start_time and end_time
        """
        # Calculate time boundaries for events (now to 1 week in the future)
        now = datetime.now(timezone.utc)
        future = now + timedelta(weeks=1)

        # Fetch events from the calendar
        events_result = (
            self.service.events()
            .list(
                calendarId=self.google_calendar_id,
                timeMin=now.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                timeMax=future.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                singleEvents=True,
                orderBy="startTime",
                timeZone="UTC",
            )
            .execute()
        )

        events = events_result.get("items", [])
        schedule = {}

        for event in events:
            # Sanitize the description (model key) using existing sanitize function
            model_key = self.sanitize(event["description"])

            # Handle both all-day events and events with specific times
            if "dateTime" in event["start"]:
                # Event with specific time
                start_time = datetime.fromisoformat(
                    event["start"]["dateTime"].replace("Z", "+00:00")
                )
                end_time = datetime.fromisoformat(
                    event["end"]["dateTime"].replace("Z", "+00:00")
                )
            else:
                # All-day event
                start_time = datetime.fromisoformat(event["start"]["date"])
                end_time = datetime.fromisoformat(event["end"]["date"])
                # Set times to start and end of day
                start_time = start_time.replace(
                    hour=0, minute=0, second=0, tzinfo=timezone.utc
                )
                # For all-day events, Google Calendar returns the next day as the end date
                # So we need to subtract one day to get the actual end time
                end_time = (end_time - timedelta(days=1)).replace(
                    hour=23, minute=59, second=59, tzinfo=timezone.utc
                )

            # Get the event title
            event_title = event.get("summary", model_key)

            schedule[model_key] = {
                "start_time": start_time,
                "end_time": end_time,
                "title": event_title,
            }
        return schedule
