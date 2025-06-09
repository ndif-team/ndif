import datetime
import re
import time
from typing import Optional

import ray
from google.oauth2 import service_account
from googleapiclient.discovery import build
from ray.serve.handle import DeploymentHandle

from .....logging.logger import load_logger

SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]

LOGGER = load_logger("Scheduler")


@ray.remote(num_cpus=1)
class SchedulingActor:

    def __init__(
        self,
        controller_handle: DeploymentHandle,
        google_creds_path: str,
        google_calendar_id: str,
        check_interval_s: float,
    ):

        credentials = service_account.Credentials.from_service_account_file(
            google_creds_path, scopes=SCOPES
        )

        self.controller_handle = controller_handle
        self.google_service = build("calendar", "v3", credentials=credentials)
        self.google_calendar_id = google_calendar_id
        self.check_interval_s = check_interval_s

        self.state_hash = hash("")

    def start(self):

        LOGGER.info("Starting Scheduler...")

        while True:
            self.check_calendar()

            time.sleep(self.check_interval_s)

    def check_calendar(self):

        start = datetime.datetime.now(datetime.timezone.utc)
        end = start + datetime.timedelta(seconds=1)

        events_result = (
            self.google_service.events()
            .list(
                calendarId=self.google_calendar_id,
                timeMin=start.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                timeMax=end.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                maxResults=100,
                singleEvents=True,
                orderBy="startTime",
            )
            .execute()
        )
        events = events_result.get("items", [])

        def sanitize(description: str):

            description = description.replace("\n", "")

            CLEANR = re.compile("<.*?>")
            description = re.sub(CLEANR, "", description)
            return description

        model_keys = sorted([sanitize(event["description"]) for event in events])

        state_hash = hash("".join(model_keys))

        if self.state_hash != state_hash:

            LOGGER.info(
                f"Change in model deployment state. Sending deployment request to Controller with model keys: {model_keys}",
            )

            self.state_hash = state_hash

            self.controller_handle.deploy.remote(model_keys, dedicated=True)