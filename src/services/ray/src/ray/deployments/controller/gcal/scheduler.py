import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import ray
from google.oauth2 import service_account
from googleapiclient.discovery import build
from .....schema.deployment_config import DeploymentConfig
from .....types import MODEL_KEY
from .....providers.mailgun import MailgunProvider
from ..cluster.node import CandidateLevel

logger = logging.getLogger("ndif")


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
        self.controller_handle = ray.get_actor("Controller", namespace="NDIF")

        # Store the hash of previous model keys to detect changes
        self.previous_model_keys_hash = hash("")

        # Google API authorization scopes
        scopes = ["https://www.googleapis.com/auth/calendar"]

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

        logger.info("Starting scheduler...")

        while True:
            try:
                await self.check_calendar()
            except Exception as e:
                import traceback

                logger.error(f"Error in check_calendar: {e}")
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
        description = description.replace("&quot;", "")
        CLEANR = re.compile("<.*?>")
        description = re.sub(CLEANR, "", description)
        return description

    async def check_calendar(self):
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

        model_keys_to_event = {
            self.sanitize(event["description"]): event for event in events
        }
        model_keys_to_event = dict(
            sorted(model_keys_to_event.items(), key=lambda item: item[0])
        )

        # Generate a hash of the model keys to check for changes
        current_hash = hash("".join(model_keys_to_event.keys()))

        # Only update if the model keys have changed
        if current_hash != self.previous_model_keys_hash:
            logger.info(
                "Change in model deployment state. Sending deployment request to Controller..."
            )
            # Update the controller with new model keys
            configs = {key: DeploymentConfig(dedicated=True) for key in model_keys_to_event.keys()}
            result: Dict[str, str] = await self.controller_handle.deploy.remote(
                configs
            )
            result = result["result"]
            error_messages = {
                model_key: value
                for model_key, value in result.items()
                if value not in CandidateLevel.__members__
            }

            # Only update the stored hash if there were no errors
            cant_accomodate = False
            for model_key, value in result.items():
                print("value: ", value)
                if value == CandidateLevel.CANT_ACCOMMODATE.name:
                    print("cant accomate: ", model_key)
                    cant_accomodate = True
                    break

            if not error_messages and not cant_accomodate:
                self.previous_model_keys_hash = current_hash

            # For successful deployments, set event color to dark green if not already
            DARK_GREEN_COLOR_ID = "10"  # Google Calendar 'Basil' (dark green)
            successful_model_keys = [
                k for k in model_keys_to_event.keys() if k not in error_messages
            ]
            for model_key in successful_model_keys:
                event = model_keys_to_event[model_key]
                event_id = event["id"]
                current_color = event.get("colorId")
                if current_color != DARK_GREEN_COLOR_ID:
                    self.service.events().patch(
                        calendarId=self.google_calendar_id,
                        eventId=event_id,
                        body={
                            "colorId": DARK_GREEN_COLOR_ID,
                        },
                        sendUpdates="all",
                    ).execute()
                    logger.info(f"Updated event {event_id} to dark green.")

            # Loop through error messages and update the corresponding calendar event
            for model_key, error_message in error_messages.items():
                # Find the event with the matching sanitized description
                logger.error(
                    f"Model key '{model_key}' encountered error: {error_message}"
                )
                event = model_keys_to_event[model_key]
                event_id = event["id"]
                # Prepend "ERROR:" to the event summary/title
                summary = event.get("summary", "")
                if not summary.startswith("ERROR:"):
                    summary = f"ERROR: {summary}"
                else:
                    summary = summary
                # Update the event with the error message in the description and new summary
                try:
                    # Ensure the creator is an attendee
                    creator_email = event.get("creator", {}).get("email", None)
                    if creator_email and MailgunProvider.connected():
                        MailgunProvider.send_email(
                            creator_email,
                            f"NDIF Scheduling Error for {summary}",
                            error_message,
                        )

                    self.service.events().patch(
                        calendarId=self.google_calendar_id,
                        eventId=event_id,
                        body={
                            "summary": summary.removeprefix("ERROR: "),
                            "colorId": "11",
                        },
                        sendUpdates="all",
                    ).execute()
                    logger.info(f"Updated event {event_id} with error message.")
                except Exception as e:
                    logger.error(f"Failed to update event {event_id} with error: {e}")
                break

    async def get_schedule(self) -> Dict[MODEL_KEY, Dict[str, Any]]:
        """
        Get scheduled events for the next week.

        Returns:
            Dict[MODEL_KEY, Dict]: A dictionary mapping model_keys to their schedule information
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

            if model_key in schedule:
                # If the event's start time minus 10 seconds is before the stored end time, update the end time.
                existing_entry = schedule[model_key]

                if (start_time - timedelta(seconds=10)) <= existing_entry["end_time"]:
                    # Combine: update the end_time to the later end_time
                    if end_time > existing_entry["end_time"]:
                        existing_entry["end_time"] = end_time
                    # Optionally, you may want to update the title if it is more descriptive
                    if event_title != existing_entry["title"]:
                        existing_entry["title"] = event_title
                    schedule[model_key] = existing_entry

            else:
                schedule[model_key] = {
                    "start_time": start_time,
                    "end_time": end_time,
                    "title": event_title,
                }
        return schedule
