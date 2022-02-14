#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import logging, sys, json, locale, datetime, base64, re
import requests
from os import getenv, environ
from pathlib import Path
from typing import Optional
from googleapiclient import discovery, errors
from google.auth import default
from google.auth.transport.requests import Request as googleRequest
from google.auth.jwt import Credentials
from google.oauth2 import service_account
from google.cloud import iam_credentials_v1

# Lookup Project Number for given ProjectID
# requires resourcemanager.projects.get permissions
def getGoogleProjectNumber(project_id: str) -> Optional[str]:
   """Lookup Project Number for gives ProjectID
   
   Args:
      project_id (str): Google Project ID
      
   Returns:
      str: Google Project Number
   
   When running inside a Google VM, App, Function etc, it will use VM Metadata to
   resolve projectID to projectNumber, else it will use
   https://cloud.google.com/resource-manager/reference/rest/v1/projects/get,
   which requires resourcemanager.projects.get permissions.
   """

   # First try to fetch from Google VM Metadata
   try:
      metadata_project_ID = requests.get("http://metadata.google.internal/computeMetadata/v1/project/project-id", headers={'Metadata-Flavor': 'Google'}).text
      metadata_project_number = requests.get("http://metadata.google.internal/computeMetadata/v1/project/numeric-project-id", headers={'Metadata-Flavor': 'Google'}).text

      if project_id == metadata_project_ID:
         return metadata_project_number
   except:
      pass

   # No metadata available, lets use resource manager
   credentials, _ = default()

   service = discovery.build('cloudresourcemanager', 'v1', credentials=credentials)

   request = service.projects().get(projectId=project_id)
   try:
      response = request.execute()
      return response["projectNumber"]
   except errors.HttpError as e:
      # Unable to resolve project. No permission or project doesn't exist
      logging.error(f"Cannot use cloudresourcemanager to resolve projectId {project_id} to project number. Missing 'resourcemanager.projects.get' permissions? ")
      pass

   logging.error(f"Cannot resolve {project_id} to project number")
   return None

# Check if string is base64 encoded
def isBase64(sb):
    try:
        if isinstance(sb, str):
            # If there's any unicode here, an exception will be thrown and the function will return false
            sb_bytes = bytes(sb, 'ascii')
        elif isinstance(sb, bytes):
            sb_bytes = sb
        else:
            raise ValueError("Argument must be string or bytes")
        return base64.b64encode(base64.b64decode(sb_bytes)) == sb_bytes
    except Exception:
        return False

class BearerAuth(requests.auth.AuthBase):
    """ Stores, passes und refreshes JWT tokens for CVS. Use with with requests 'auth' parameter. """
    credentials = None
    projectID = None

    def __init__(self, service_account_identifier):
        """ Initialize token

        Args:
            service_account_identifier (str): service account  with cloudvolumes.* permissions
                    Can be specified in multiple ways:
                    1. Absolute file path to an JSON key file
                    2. JSON key as base64-encoded string
                    3. Service Account principal name when using service account impersonation

        It raises a ValueError if key provided isn't a valid JSON key.
        """
        audience = 'https://cloudvolumesgcp-api.netapp.com'

        # Check if we got a passed a user-managed service account principal
        # Format: <service_account_name>@<project_id>.iam.gserviceaccount.com
        user_managed_sa_regex = "^[a-z]([-a-z0-9]*[a-z0-9])@[a-z0-9-]+\.iam\.gserviceaccount\.com$"
        if re.match(user_managed_sa_regex, service_account_identifier):
            self.projectID = service_account_identifier.split('@')[1].split('.')[0]
            self.credentials = self.ImpersonationCreds(service_account_identifier)
        else:
            # check if we got passed a path to a service account JSON key file
            # or we got passed the key itself encoded base64
            if isBase64(service_account_identifier):
                # we got passed an base64 encoded JSON key
                json_key = json.loads(base64.b64decode(service_account_identifier))
            else:
                # we got passed an file path to an JSON key file
                file_path = Path(service_account_identifier)
                if file_path.is_file():
                    with open(file_path, 'r') as file:
                        json_key = json.loads(file.read())
                else:
                    logging.error('Passed credentials are not a base64 encoded json key nor a vaild file path to a keyfile.')
                    raise ValueError('Passed credentials are not a base64 encoded json key nor a vaild file path to a keyfile.')

            self.projectID = json_key['project_id']
            self.credentials = self.JSONKeyCreds(json_key)

    def __call__(self, r):
        r.headers["authorization"] = "Bearer " + self.credentials.get_token()
        return r

    def __str__(self):
        return self.credentials.get_token()

    def getProjectID(self):
        """ Returns projectID fetched from JSON key """
        return self.projectID

    class ImpersonationCreds:
        # Internal helper class for Service Account Impersonation auth
        expiry = datetime.datetime.now()
        token = None
        service_account_name = None
        token_life_time = 5*60  # 5 Minutes

        def __init__(self, service_account_name: str):
            self.service_account_name = service_account_name
            self._new_token()

        def get_token(self) -> str:
            if datetime.datetime.now() + datetime.timedelta(seconds=10) > self.expiry:
                # Token to expire within 10s. Let's refresh
                self._new_token()
            return self.token

        def _new_token(self):
            audience = 'https://cloudvolumesgcp-api.netapp.com'

            now = datetime.datetime.now()
            self.expiry = now + datetime.timedelta(seconds = self.token_life_time)

            claims = {
                "iss": self.service_account_name,
                "sub": self.service_account_name,
                "iat": int(now.timestamp()),
                "exp": int(self.expiry.timestamp()),
                "aud": audience,
            }

            client = iam_credentials_v1.IAMCredentialsClient()
            service_account_path = client.service_account_path('-', self.service_account_name)
            response = client.sign_jwt(request = { "name": service_account_path, "payload": json.dumps(claims) })
            self.token = response.signed_jwt

    class JSONKeyCreds:
        # Internal helper class for JSON key auth
        credentials = None

        def __init__(self, json_key: str):
            audience = 'https://cloudvolumesgcp-api.netapp.com'

            svc_creds = service_account.Credentials.from_service_account_info(json_key)
            jwt_creds = Credentials.from_signing_credentials(svc_creds, audience=audience)
            jwt_creds.refresh(googleRequest())

            self.credentials = jwt_creds

        def get_token(self) -> str:
            if self.credentials.expired:
                self.credentials.refresh(googleRequest())
            return self.credentials.token.decode('utf-8')


# Class to handle CVS API calls
class GCPCVS():
    project: str = None
    projectId: str = None
    service_account: str = None
    token: BearerAuth = None
    baseurl: str = None
    headers: dict = {
                "Content-Type": "application/json",
                "User-Agent": "CVSCapacityManager"
            }

    def __init__(self, service_account: str, project: str = None):
        """
        Args:
            service_account (str): service account key with cloudvolumes.admin permissions
                Can be specified in multiple ways:
                1. Absolute file path to an JSON key file
                2. JSON key as base64-encoded string
                3. Service Account principal name when using service account impersonation
            project (str): Google project_number or project_id or None
                If "None", project_id is fetched from service_account
                If using project_id, resourcemanager.projects.get permissions are required
        """

        self.service_account = service_account
        self.token = BearerAuth(service_account)    # Will raise ValueError is key provided is invalid

        if project == None:
            # Fetch projectID from JSON key file
            project = self.token.getProjectID()

        # Initialize projectID. Its is now either a valid projectId, or at least the project number
        self.projectId = project
        # Resolve projectID to projectNumber
        if re.match(r"[a-zA-z][a-zA-Z0-9-]+", project):
            project = getGoogleProjectNumber(project)
            if project == None:
                raise ValueError("Cannot resolve projectId to project number. Please specify project number.")
        self.project = project

        self.baseurl = 'https://cloudvolumesgcp-api.netapp.com/v2/projects/' + str(self.project)

    # print some infos on the class
    def __str__(self) -> str:
        return f"CVS: Project: {self.project}\nService Account: {self.service_account}\n"

    # returns list with dicts of all volumes in specified region ("-" for all regions)
    def getVolumesByRegion(self, region: str) -> list:
        logging.info(f"getVolumesByRegion {region}")
        r = requests.get(f"{self.baseurl}/locations/{region}/Volumes", headers=self.headers, auth=self.token)
        r.raise_for_status()
        return r.json()

    # returns volumes with volumeID in specified region
    def getVolumesByVolumeID(self, region: str, volumeID: str) -> Optional[dict]:
        logging.info(f"getVolumesByVolumeID {region}, {volumeID}")
        r = requests.get(f"{self.baseurl}/locations/{region}/Volumes", headers=self.headers, auth=self.token)
        r.raise_for_status()
        vols = [volume for volume in r.json() if volume["volumeId"] == volumeID]
        if len(vols) == 1:
            return vols[0]
        else:
            return None

    # modify a volume
    # pass in dict with API field to modify
    # used by more specialized methods
    def _modifyVolumeByVolumeID(self, region: str, volumeID: str, changes: dict) -> dict:
        logging.info(f"modifyVolumeByVolumeID {region}, {volumeID}, {changes}")
        # read volume
        r = requests.get(f"{self.baseurl}/locations/{region}/Volumes/{volumeID}", headers=self.headers, auth=self.token)
        r.raise_for_status()
        volume = r.json()
        for k in changes:
            volume[k] = changes[k]
        r = requests.put(f"{self.baseurl}/locations/{region}/Volumes/{volumeID}", headers=self.headers, auth=self.token, json=volume)
        r.raise_for_status()
        return r.json()
    
    # change size of volume
    def resizeVolumeByVolumeID(self, region: str, volumeID: str, newSize: int):
        logging.info(f"updateVolumeByVolumeID {region}, {volumeID}, {newSize}")
        self._modifyVolumeByVolumeID(region, volumeID, {"quotaInBytes": newSize})

    # CVS API uses serviceLevel = (basic, standard, extreme)
    # CVS UI uses serviceLevel = (standard, premium, extreme)
    # yes, the name "standard" has two different meaning *sic*
    # CVS-SO uses serviceLevel = basic, storageClass = software and regional_ha=(true|false) and
    # for simplicity reasons we translate it to serviceLevel = standard-sw
    def translateServiceLevelAPI2UI(self, serviceLevel: str) -> Optional[str]:
        serviceLevelsAPI = {
            "basic": "standard",
            "standard": "premium",
            "extreme": "extreme",
            "standard-sw": "standard-sw"
        }
        if serviceLevel in serviceLevelsAPI:
            return serviceLevelsAPI[serviceLevel]
        else:
            logging.warning(f"translateServiceLevelAPI2UI: Unknown serviceLevel {serviceLevel}")
            return None

    def translateServiceLevelUI2API(self, serviceLevel: str) -> Optional[str]:
        serviceLevelsUI = {
            "standard": "basic",
            "premium": "standard",
            "extreme": "extreme",
            "standard-sw": "standard-sw"
        }
        if serviceLevel in serviceLevelsUI:
            return serviceLevelsUI[serviceLevel]
        else:
            logging.warning(f"translateServiceLevelUI2API: Unknown serviceLevel {serviceLevel}")
            return None

# Calculate new recommended size of volume
# Since each volume got a performance limit per capacity (QoS), we can
# calculate how quickly the free space can be consumed at max.
# If we know the time interval this script is running, we can make the volume
# big enough so it doesn't run out of space meanwhile
# Parameters:
#  size = current volume size in B
#  serviceLevel = name of CVS serviceLevel (basic, standard, extreme, standard-sw)
#  duration = time in minutes between script runs
#  margin = add additional capacity security margin on top in %
# Result:
#  New proposed size in B, rounded up to align to full GiB
def calculateNewCapacity(size: int, serviceLevel: str, duration: int, margin: int) -> int:
    logging.info(f"calculateNewCapacity {size}, {serviceLevel}, {duration}, {margin}")

    qos = { 'basic' : 16,
            'standard': 64,
            'extreme': 128,
            'standard-sw': 128
            }

    if serviceLevel in qos:
        speed = qos[serviceLevel]
    else:
        logging.warning(f'calculateNewCapacity: Unknown serviceType: {serviceLevel}. Using "extreme"')
        speed = qos['extreme']
    
    speed = int(speed) # Linter of Cloud Function is on drugs

    # Calculate the new volume size
    # Formula takes into consideration that bigger volume = more speed = quicker fill rate
    newSize = int( -size / ( duration * 60 * speed / 1024**2 * (1 + margin / 100) - 1) )
    # Round up to full GiB
    newSize = int(newSize / 1024**3 + 1) * 1024**3

    return newSize

# Resize all volume of the project
def resize(project_id: str, service_account_credential: str, duration:int, margin: int, outputJSON: bool, dry_mode: bool):
    cvs = GCPCVS(service_account_credential, project_id)
    
    # Query all CVS volumes in project
    allvolumes = cvs.getVolumesByRegion("-")
    if outputJSON == False:
        print(f'{"Name":30} {"serviceLevel":12} {"used [B]":>22} {"allocated [B]":>22} {"snapReserve":11} {"%used":5} {"new_allocated [B]":>22} {"Resize"}')

    for volume in allvolumes:
        resizeVolume(cvs, volume, duration, margin, outputJSON, dry_mode)

# Resizes a volume
# If duration is 0, newSize = used * 100 / (100 - margin). Basically, leave "margin%" free space after resize
# If duraction <> 0, use dynamic size calculation, see calculateNewCapacity
def resizeVolume(cvs, volume: dict, duration:int, margin: int, outputJSON: bool, dry_mode: bool) -> bool:
    name = volume["name"]
    quota = int(volume["quotaInBytes"])
    used = int(volume["usedBytes"])
    snapReserve = int(volume["snapReserve"])

    # skip volumes which are not available
    if volume['lifeCycleState'] != 'available':
        if outputJSON == True:
            print(json.dumps({'severity': "INFO", 'volume': name, 'message': "Volume is not available. Skipping ..."}))
        else:
            print(f'{name:30} {"Volume is not available. Skipping ..."}')
        return False

    # active CRR Secondary volumes are resized by resizing the primary volume. Ignore
    if volume['isDataProtection'] == True and volume['inReplication'] == True:
        if outputJSON == True:
            print(json.dumps({'severity': "INFO", 'volume': name, 'message': "Secondary volume in active replication. Skipping ..."}))
        else:
            print(f'{name:30} {"Secondary volume in active replication. Skipping ..."}')
        return False

    # CVS-standard-sw uses serviceLevel = "basic", which deliver 128 KiB/s/GiB
    # CVS-performance serviceLevel = "basic" is "Standard", which delivers 16 KiB/s/GiB
    # to distinguish both kinds of "basic", call the CVS-standard-sw one "standard-sw"
    if volume["storageClass"] == "hardware":
        serviceLevel = volume["serviceLevel"]
    else:
        serviceLevel = "standard-sw"

    # Calculate new size
    if duration == 0:
        # Using static margin
        newSize = int(used * 100 / (100 - margin))
        # Round up to full GiB
        newSize = int(newSize / 1024**3 + 1) * 1024**3
    else:
        # Using dynamic size, dependent on volume size, servicelevel etc
        newSize = calculateNewCapacity(used, serviceLevel, duration, margin)

    # Do we need to resize the volume?
    if newSize > quota:
        enlarge = True
        # max volume size is 100TiB. cap at 100TiB
        if newSize > 100*1024**4:
            if outputJSON == True:
                print(json.dumps({'severity': "WARNING", 'volume': name, 'message': "Resizing capped to 100 TiB"}))
            else:
                print("Resizing capped to 100 TiB")
            newSize = 100*1024**4
    else:
        newSize = quota
        enlarge = False

    # Print volume list with relevant data
    if outputJSON == True:
        # Structured logging
        entry = dict(
            severity = "INFO",
            volume = name,
            region = volume["region"],
            UUID = volume["volumeId"],
            serviceLevel = cvs.translateServiceLevelAPI2UI(serviceLevel),
            oldSize = used,
            quota = quota,
            enlarge = enlarge,
            newSize = newSize,
            snapReserve = snapReserve
        )
        print(json.dumps(entry))
    else:
        print(f'{name:30} {cvs.translateServiceLevelAPI2UI(serviceLevel):12} {used:22n} {quota:22n} {snapReserve:11n} {round(used / quota * 100, 1):5n} {newSize:22n} {"Yes" if enlarge else ""}')

    if enlarge == True and dry_mode == False:
        # Volume needs resizing. Call API
        cvs.resizeVolumeByVolumeID(volume["region"], volume["volumeId"], newSize)
        return True
    return False

def CVSCapacityManager_alert_event(event, context):
    """ Receives PubSub messages from Cloud alerts and resizes volume
    
        see /Alerting/cvs-Alerting.tf for alert definition
        
        Parameters passed via environment:
            SERVICE_ACCOUNT_CREDENTIAL = base64 encoded content of JSON key (cat json.key | base64)
            CVS_CAPACITY_MARGIN = % of free capacity requested, comapred to current volume allocation. Default 20
            CVS_DRY_MODE = optional parameter. If present, only report, but don't resize
        Parameters passed via PubSub:
            data.incident.resource.project_id = ProjectId - only used for reporting
            data.incident.resource.resource_container = ProjectNumber - used for API calls
            data.incident.resource.location = region - used for API calls
            data.incident.resource.volume_id = volume_id - used for API calls
            data.incident.resource.name = volume name - only used for reporting     
    """

    logging.basicConfig(stream=sys.stdout, level=logging.WARNING)

    # If environment variables are set, we use environment for parameters instead of JSON payload
    if 'SERVICE_ACCOUNT_CREDENTIAL' in environ:
        service_account = getenv('SERVICE_ACCOUNT_CREDENTIAL', None)
        margin = int(getenv('CVS_CAPACITY_MARGIN', 20))
        if 'CVS_DRY_MODE' in environ:
            dry_mode = True
        else:
            dry_mode = False

        print(json.dumps({'parameter_source': 'environment', 'margin': margin, 'dry_mode': dry_mode, 'service_account': service_account[0:9] + "..."}))

        # Get volume detail from alert event
        if 'data' in event:
            payload = json.loads(base64.b64decode(event['data']).decode('utf-8'))

            try:                    
                parameters = payload['incident']['resource']['labels']
                project_id = parameters['project_id']
                project_number = parameters['resource_container']
                region = parameters['location']
                volume_id = parameters['volume_id']
                volumeName = parameters['name']
                if payload['incident']['state'] == "closed":
                    print(json.dumps({'severity': "INFO", 'name': volumeName, 'UUID': volume_id, 'message': "Incident resolved"}))
                    return
            except KeyError:
                print(json.dumps({'severity': "ERROR", 'message': "PubSub payload is missing parameters. Is it really a Cloud Monitoring alert?"}))
                return "PubSub payload is missing parameters. Is it really a Cloud Monitoring alert?", 400

            print(json.dumps({'parameter_source': 'pubsub', 'project_id': project_id, 'project_number': project_number, 'region': region, 'name': volumeName, 'UUID': volume_id}))

            # resize the volume
            # Volume needs resizing. Call API
            cvs = GCPCVS(service_account, project_id)
            volume = cvs.getVolumesByVolumeID(region, volume_id)
            if volume == None:
                print(json.dumps({'severity': "ERROR", 'message': f"Cannot find volume {volume_id} in region {region}"}))
                return f"Cannot find volume {volume_id} in region {region}", 400
            resizeVolume(cvs, volume, 0, margin, True, dry_mode)
        else:
            print(json.dumps({'severity': "WARNING", 'message': "No Alert received. Assuming Cloud Function Test. DRY_MODE enabled."}))
            resize(None, service_account, 0, margin, True, True)
        return
        
    print(json.dumps({'severity': "ERROR", 'message': "Missing environment parameters - no action"}))
    return "Missing environment parameters - no action", 400


def CVSCapacityManager_pubsub(event, context):
    """ PubSub receiver function for Cloud Function

        Receives an envent via PubSub. Will query all volumes in the project and
        resize them if they are might run full until the next invocation

        Parameters passed via environment:
            DEVSHELL_PROJECT_ID = projectId or projectNumber
            SERVICE_ACCOUNT_CREDENTIAL = base64 encoded content of JSON key (cat json.key | base64)
            CVS_CAPACITY_INTERVAL = Time in minutes between invocations
            CVS_CAPACITY_MARGIN = Capacity in % to add on current volume allocation
            CVS_DRY_MODE = optional parameter. If present, only report, but don't resize

        It also supports a way to pass parameters using a JSON payload. DEPRECATED. Please stop using it.
    """

    logging.basicConfig(stream=sys.stdout, level=logging.WARNING)

    # If environment variables are set, we use environment for parameters instead of JSON payload
    if {'SERVICE_ACCOUNT_CREDENTIAL'} <= set(environ):
        project_id = getenv('DEVSHELL_PROJECT_ID', None)
        service_account = getenv('SERVICE_ACCOUNT_CREDENTIAL', None)
        duration = int(getenv('CVS_CAPACITY_INTERVAL', 60))
        margin = int(getenv('CVS_CAPACITY_MARGIN', 20))
        if 'CVS_DRY_MODE' in environ:
            dry_mode = True
        else:
            dry_mode = False

        print(json.dumps({'parameter_mode': 'environment', 'project_id': project_id, 'duration': duration, 'margin': margin, 'dry_mode': dry_mode, 'service_account': service_account[0:9] + "..."}))
        resize(project_id, service_account, duration, margin, True, dry_mode)
        return      

    # legacy way using JSON payload. DEPRECATED
    if 'data' in event:
        parameters = json.loads(base64.b64decode(event['data']).decode('utf-8'))
        
        try:
            project_id = parameters['projectid']
            duration = int(parameters['duration'])
            margin = int(parameters['margin'])
            service_account = parameters['service_account']
            if 'dry_mode' in parameters:
                dry_mode = True
            else:
                dry_mode = False
        except KeyError:
            return "JSON payload: Missing parameter", 400

        # for service_account, only the first 10 characters are printed
        print(json.dumps({'parameter_mode': 'payload', 'project_id': project_id, 'duration': duration, 'margin': margin, 'dry_mode': dry_mode, 'service_account': service_account[0:9] + "..."}))
        resize(project_id, service_account, duration, margin, True, dry_mode)
        return

    return "Missing parameters - no action", 400

def CVSCapacityManager_cli():
    locale.setlocale(locale.LC_ALL, 'en_US.UTF-8')
    if 'CVS_DEBUGGING' in environ:
        logging.basicConfig(stream=sys.stdout, level=logging.INFO)
    else:
        logging.basicConfig(stream=sys.stdout, level=logging.WARNING)    

    # Set this variables for your environment
    project_id = getenv('DEVSHELL_PROJECT_ID', None)

    # if project_id == None:
    #     logging.error('ProjectID not set in DEVSHELL_PROJECT_ID. Try "export DEVSHELL_PROJECT_ID=$(gcloud config get-value project)"')
    #     sys.exit(1)
    # else:
    #     print("Project:", project_id)

    # check if file with service account credentials exists
    service_account_credential = getenv('SERVICE_ACCOUNT_CREDENTIAL', None)
    if service_account_credential == None:
        logging.error('Missing service account credentials. Please set provide file path to JSON key file or provide credentials like "export SERVICE_ACCOUNT_CREDENTIAL=$(cat key.json | base64)"')
        sys.exit(2)

    # Amount of spare capacity in % (e.g. 10 = 10%) to add on top of calculated target capacity
    margin = int(getenv('CVS_CAPACITY_MARGIN', 20))
    # Tell script how often it is ran. Duration in minutes
    duration = int(getenv('CVS_CAPACITY_INTERVAL', 60))
    # Dry mode. Report everything, but don't change volume sizes
    if 'CVS_DRY_MODE' in environ:
        dry_mode = True
    else:
        dry_mode = False

    print(f"Parameters: CVS_CAPACITY_INTERVAL: {duration} minutes, CVS_CAPACITY_MARGIN: {margin}%, CVS_DRY_MODE: {dry_mode}")

    resize(project_id, service_account_credential, duration, margin, False, dry_mode)

if __name__ == "__main__":
    CVSCapacityManager_cli()
