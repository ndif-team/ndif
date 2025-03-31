import hashlib
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Any

import ray
from ray.serve import DeploymentHandle
from googleapiclient.discovery import build
from google.oauth2 import service_account


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
        check_interval: float,
        controller_handle: DeploymentHandle,
    ):
        """
        Initialize the SchedulingActor.
        
        Args:
            google_credentials_path: Path to the Google API service account credentials file
            google_calendar_id: ID of the Google Calendar to monitor
            check_interval: Time in seconds between calendar checks
            controller_handle: Handle to the SchedulingControllerDeployment
        """
        self.google_credentials_path = google_credentials_path
        self.google_calendar_id = google_calendar_id
        self.check_interval = check_interval
        self.controller_handle = controller_handle
        
        # Store the hash of previous model keys to detect changes
        self.previous_model_keys_hash = hash("")
        
        # Google API authorization scopes
        scopes = ['https://www.googleapis.com/auth/calendar.readonly']

        # Create credentials from the service account file
        credentials = service_account.Credentials.from_service_account_file(
            self.google_credentials_path, scopes=scopes
        )
        
        # Build the Google Calendar API service
        self.service = build('calendar', 'v3', credentials=credentials)
       
        
    def start(self):
        """
        Start the scheduling loop.
        
        This method continuously checks the calendar for changes at the 
        specified interval.
        """
        
        print("Starting scheduler...")
        
        while True:
     
            self.check_calendar()

            # Wait for the next check interval
            time.sleep(self.check_interval)
    
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
        events_result = self.service.events().list(
            calendarId=self.google_calendar_id,
            timeMin=now.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            timeMax=future.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            singleEvents=True,
            orderBy='startTime'
        ).execute()
        
        events = events_result.get('items', [])
        
        def sanitize(description: str):

            description = description.replace("\n", "")

            CLEANR = re.compile("<.*?>")
            description = re.sub(CLEANR, "", description)
            return description

        model_keys = sorted([sanitize(event["description"]) for event in events])
        
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
            self.controller_handle.deploy.remote(model_keys)

            
