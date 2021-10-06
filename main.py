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

import logging
import sys
import json
import locale
from os import getenv, environ
import base64
import requests
import re
from googleapiclient import discovery, errors
from google.auth import default
from google.auth.transport.requests import Request as googleRequest
from google.auth.jwt import Credentials
from google.oauth2 import service_account

# Lookup Project Number for given ProjectID
# requires resourcemanager.projects.get permissions
def getGoogleProjectNumber(project_id: str) -> str:
   credentials, _ = default()

   service = discovery.build('cloudresourcemanager', 'v1', credentials=credentials)

   request = service.projects().get(projectId=project_id)
   try:
      response = request.execute()
      return response["projectNumber"]
   except errors.HttpError as e:
      # Unable to resolve project. No permission or project doesn't exist
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
    credentials = None

    def __init__(self, sa_key):
        audience = 'https://cloudvolumesgcp-api.netapp.com'
        # check if we got a path to a service account JSON key file
        # or we got the key itself encoded base64
        if isBase64(sa_key):
            # we got an base64 encoded JSON key
            svc_creds = service_account.Credentials.from_service_account_info(json.loads(base64.b64decode(sa_key)))
        else:
            # we got an file path to an JSON key file
            svc_creds = service_account.Credentials.from_service_account_file(sa_key)
        jwt_creds = Credentials.from_signing_credentials(svc_creds, audience=audience)
        request = googleRequest()
        jwt_creds.refresh(request)

        self.credentials = jwt_creds
        # self.expires_at = datetime.datetime.now() + datetime.timedelta(0, r.json()['expires_in'])

    def __call__(self, r):
        # Add expiration check here
        r.headers["authorization"] = "Bearer " + self.credentials.token.decode('utf-8')
        return r

    def __str__(self):
        return self.credentials.token

# Class to handle CVS API calls
class GCPCVS():
    project: str = None
    projectId: str = None
    service_account: str = None
    token: BearerAuth = None
    baseurl: str = None
    headers: dict = {
                "Content-Type": "application/json"
            }

    # Initializes the object
    # IN:
    #   project = GCP project (projectNumber or  projectID)
    #   service_account = either base64 encodes service account key or path to key JSON file
    #                     service account needs cloudvolumes.admin permissions
    def __init__(self, project: str, service_account: str):
        # Resolve projectID to projectNumber
        if re.match(r"[a-zA-z][a-zA-Z0-9-]+", project):
            self.projectId = project
            project = getGoogleProjectNumber(project)
            if project == None:
                raise ValueError("Cannot resolve projectId to project number. Please specify project number.")                
        self.project = project
        self.service_account = service_account

        self.baseurl = 'https://cloudvolumesgcp-api.netapp.com/v2/projects/' + str(self.project)
        self.token = BearerAuth(service_account)

    # print some infos on the class
    def __str__(self) -> str:
        return f"CVS: Project: {self.project}\nService Account: {self.service_account}\n"

    # returns list with dicts of all volumes in specified region ("-" for all regions)
    def getVolumesByRegion(self, region: str) -> list:
        logging.info(f"getVolumesByRegion {region}")
        r = requests.get(f"{self.baseurl}/locations/{region}/Volumes", headers=self.headers, auth=self.token)
        r.raise_for_status()
        return r.json()
        
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
    def translateServiceLevelAPI2UI(self, serviceLevel: str) -> str:
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

    def translateServiceLevelUI2API(self, serviceLevel: str) -> str:
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
        logging.warning(f'calculateNewCapacity: Unknown serviceType: {serviceLevel}. Using "high"')
        speed = qos['high']
        
    # Calculate the new volume size
    # Formula takes into consideration that bigger volume = more speed = quicker fill rate
    newSize = int( -size / ( duration * 60 * speed / 1024**2 * (1 + margin / 100) - 1) )
    # Round up to full GiB
    newSize = int(newSize / 1024**3 + 1) * 1024**3

    return newSize

# Query all volumes in the project. For each volume, calculate how big it can grow within <duration>
# considering the current used size and the max write speed determined by volumes serviceLevel
# If calculated_size > allocted_size, resize volume to calculated_size
def resize(project_id: str, service_account_credential: str, duration:int, margin: int, outputJSON: bool, dry_mode: bool):
    cvs = GCPCVS(project_id, service_account_credential)
    
    # Query all CVS volumes in project
    allvolumes = cvs.getVolumesByRegion("-")
    if outputJSON == False:
        print(f'{"Name":30} {"serviceLevel":12} {"used [B]":>22} {"allocated [B]":>22} {"snapReserve":11} {"%used":5} {"new_allocated [B]":>22} {"Resize"}')

    for volume in allvolumes:
        name = volume["name"]
        quota = volume["quotaInBytes"]
        used = volume["usedBytes"]
        snapReserve = volume["snapReserve"]

        # skip volumes which are not available
        if volume['lifeCycleState'] != 'available':
            if outputJSON == True:
                print(json.dumps({'severity': "INFO", 'volume': name, 'message': "Volume is not available. Skipping ..."}))
            else:
                print(f'{name:30} {"Volume is not available. Skipping ..."}')
            continue

        # active CRR Secondary volumes are resized by resizing the primary volume. Ignore
        if volume['isDataProtection'] == True and volume['inReplication'] == True:
            if outputJSON == True:
                print(json.dumps({'severity': "INFO", 'volume': name, 'message': "Secondary volume in active replication. Skipping ..."}))
            else:
                print(f'{name:30} {"Secondary volume in active replication. Skipping ..."}')
            continue

        # CVS-standard-sw uses serviceLevel = "basic", which deliver 128 KiB/s/GiB
        # CVS-performance serviceLevel = "basic" is "Standard", which delivers 16 KiB/s/GiB
        # to distinguish both kinds of "basic", call the CVS-standard-sw one "standard-sw"
        if volume["storageClass"] == "hardware":
            serviceLevel = volume["serviceLevel"]
        else:
            serviceLevel = "standard-sw"
  
        # Calculate new size
        newSize = calculateNewCapacity(used, serviceLevel, duration, margin)
        if newSize > quota:
            enlarge = True
        else:
            newSize = quota
            enlarge = False

        # Print volume list with relevant data
        if outputJSON == True:
            # Structured logging
            entry = dict(
                severity = "INFO",
                volume = name,
                serviceLevel = cvs.translateServiceLevelAPI2UI(serviceLevel),
                used = used,
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
            cvs.resizeVolumeByVolumeID("europe-west4", volume["volumeId"], newSize)

# Receiver function for Cloud Function PubSub
# expected payload in data:
#   data = {
#         "projectid":        "my-project",     # Project ID
#         "duration":         60,               # Minutes between invocations of this script
#         "margin":           10,               # Security margin of additional space to add in 0-100%
#         "service_account":  "..."             # base64 encoded content of JSON key (cat json.key | base64)
#         "dry_mode":         false             # Don't change volume sizes. Optional parameter
#         }
def CVSCapacityManager_pubsub(event, context):
    logging.basicConfig(stream=sys.stdout, level=logging.WARNING)

    if 'data' in event:
        parameters = json.loads(base64.b64decode(event['data']).decode('utf-8'))
        
        try:
            project_id = parameters['projectid']
            duration = parameters['duration']
            margin = parameters['margin']
            service_account = parameters['service_account']
            if 'dry_mode' in parameters:
                dry_mode = True
            else:
                dry_mode = False
        except KeyError:
            return "Missing parameter", 400

        # for service_account, only the first 10 characters are printed
        print(json.dumps({ 'project_id': project_id, 'duration': duration, 'margin': margin, 'dry_mode': dry_mode, 'service_account': service_account[0:9] + "..."}))
        resize(project_id, service_account, duration, margin, True, dry_mode)

def CVSCapacityManager_cli():
    locale.setlocale(locale.LC_ALL, 'en_US.UTF-8')
    if 'CVS_DEBUGGING' in environ:
        logging.basicConfig(stream=sys.stdout, level=logging.INFO)
    else:
        logging.basicConfig(stream=sys.stdout, level=logging.WARNING)    

    # Set this variables for your environment
    project_id = getenv('DEVSHELL_PROJECT_ID', None)

    if project_id == None:
        logging.error('ProjectID not set in DEVSHELL_PROJECT_ID. Try "export DEVSHELL_PROJECT_ID=$(gcloud config get-value project)"')
        sys.exit(1)
    else:
        print("Project:", project_id)

    # check if file with service account credentials exists
    service_account_credential = getenv('SERVICE_ACCOUNT_CREDENTIAL', None)
    if service_account_credential == None:
        logging.error('Missing service account credentials. Please set provide file path to JSON key file or provide credentials like "export SERVICE_ACCOUNT_CREDENTIAL=$(cat key.json | base64)"')
        sys.exit(2)

    # Amount of spare capacity in % (e.g. 10 = 10%) to add on top of calculated target capacity
    margin = getenv('CVS_CAPACITY_MARGIN', 20)
    # Tell script how often it is ran. Duration in minutes
    duration = getenv('CVS_CAPACITY_INTERVAL', 60)
    # Dry mode. Report everything, but don't change volume sizes
    if 'CVS_DRY_MODE' in environ:
        dry_mode = True
    else:
        dry_mode = False

    print(f"Parameters: CVS_CAPACITY_INTERVAL: {duration} minutes, CVS_CAPACITY_MARGIN: {margin}%, CVS_DRY_MODE: {dry_mode}")

    resize(project_id, service_account_credential, duration, margin, False, dry_mode)

    # Testcode for running the pub_sub function manually
    # payload = json.dumps({ 'projectid': project_id,
    #             'duration': duration,
    #             'margin': margin,
    #             'service_account': service_account_credential,
    #             'dry_mode': "yes"
    #             })
    # event = { 'data': base64.b64encode(payload.encode('utf-8'))}
    # CVSCapacityManager_pubsub(event, None)

if __name__ == "__main__":
    CVSCapacityManager_cli()
