# GCP-CVS-CapacityManager

A script to resize [Cloud Volumes Service (CVS)](https://cloud.google.com/architecture/partners/netapp-cloud-volumes/overview?hl=en_US) volumes on GCP to avoid running into out-of-space conditions.

## Concept
Whenever the script runs, it enumerates all volumes in the specified project. For each volume, it determines the used size (numbers of bytes consumed by data) and the quota (size of the volume as set by user).

The user  needs to specify the *interval* the script is invoked and an optional security *margin*.

Since the service level and the size of a volume determine maximum write speed, the script can calculate the maximum amount of new data which can be written to the volume within the interval (e.g every 60 minutes). This results in a new potential size. A security margin (in %) can be added (e.g. 10 for 10%) on top. If the new potential size is bigger than the current quota, the volume quota will be grown to the calculated size.

If the script is executed at the same scheduling interval specified in *duration*, the volume will always be big enough to avoid out-of-space conditions.

Using Google Cloud Scheduler, PubSub and Cloud Functions, the process can be automated.

## Usage

The script can be invoked via CLI or via a PubSub message. Independent of invokation type, 4 arguments need to be provided:

* **projectid**: GCP project ID (e.g my-project) or GCP project number (e.g. 1234567890). If project ID is specified, the script needs resourcemanager.projects.get permissions, otherwise use project number
* **duration**: Time interval in minutes the script will be called (e.g. 60 for every 60 minutes)
* **margin**: Additional security margin in %. The script calculates the new target size and will put margin % on top of it
* **service_account**: A base64 encoded JSON key for a [IAM service account](https://cloud.google.com/architecture/partners/netapp-cloud-volumes/api?hl=en_US) which has **roles/netappcloudvolumes.admin**. Use
```bash
cat key.json | base64
```
to generate it. Goal is to eventually use service account impersonation, but we need to stick with passing credentials for now

### CLI
The script takes the 4 arguments via environment variables:

```bash
# Make sure python3.6 or later environment exists on your machine
pip3 install -r requirements.txt
export DEVSHELL_PROJECT_ID=$(gcloud config get-value project)
export CVS_CAPACITY_INTERVAL=15 # default is 60 minutes
export CVS_CAPACITY_MARGIN=15 # default is 20% on top
export SERVICE_ACCOUNT_CREDENTIAL=$(cat key.json | base64)
python3 ./main.py
```

### PubSub message

The script contains a PubSub subscriber function called CVSCapacityManager_pubsub. It expects a payload like:

```json
{
        "projectid":        "my-project",
        "duration":         60,
        "margin":           20,
        "service_account":  "abcd..."
}
```

*Note*: service_account is a JSON key encoded in base64

The intended way to run it is using [Google Cloud Scheduler](https://cloud.google.com/scheduler) to trigger [Google PubSub messages](https://cloud.google.com/pubsub) which are received by this script running as [Google Cloud Function](https://cloud.google.com/functions).

```bash
# Create new PubSub topic
topic=CVSCapacityManager
gcloud pubsub topics create $topic

# Set serviceAccount to name of service account with cloudvolumes.admin permissions (see https://cloud.google.com/architecture/partners/netapp-cloud-volumes/api?hl=en_US). This can later be used for service account impersonation, but is currently defunct.
# Provide JSON key to this service account in a file named key.json
# serviceAccount=$(cat key.json | jq '.client_email')
serviceAccount="cvs-sa-svcacct@cv-solution-architect-lab.iam.gserviceaccount.com"

# Deploy Cloud Function
gcloud functions deploy CVSCapacityManager --entry-point CVSCapacityManager_pubsub --trigger-topic $topic --runtime=python39 --region=europe-west1 --service-account $serviceAccount

# Setup Cloud Scheduler
# run every hour (60 minutes), margin 20%
IFS='' read -r -d '' payload <<EOF
{
        "projectid":        "$(gcloud config get-value project)",
        "duration":         60,
        "margin":           20,
        "service_account":  "$(cat key.json | base64)"
}
EOF
gcloud scheduler jobs create pubsub CVSCapacityManager-job --schedule="0 * * * *" --time-zone="Etc/UTC" --topic=$topic --message-body=$payload
```

## Notes
* Only volumes with *lifeCycleState = available* are considered, all others are ignored
* Snapshot space handling: The script uses the usedBytes API parameter. It includes all active data, metadata and snapshot blocks
* SnapReserve handling: *snapReserve* only impacts the amount of space presented to the client via df (e.g 2000 GiB volume, snapReserve 20%, client only sees 1600 GiB). The script uses *usedBytes*, which isn't influenced by *snapReserve*
* If target volume size is larger than [maximum volume size](https://cloud.google.com/architecture/partners/netapp-cloud-volumes/resource-limits-quotas?hl=en_US), behaviour is unknown. Will either fail or resize to maximum size. Not tested.
* For host/service projects, run the script in each service/host project which provisioned volumes
* Script is developed and tested with Python 3.9

## Troubleshooting
If you run into issues, try running the script via the CLI way first, as it removes the complexity of Cloud Schedule, PubSub and CloudFunctions. Does it work? Are your parameters correct?

Setting 
```bash
export CVS_DEBUGGING=true
```
will output more information.

If CLI works, but CloudFunction doesn't, check the payload string in Cloud Scheduler first. Does it have a projectID? Have you tried project number instead? Is the service account key empty? Is it correct (try as base64 decode to see if it returns a proper key).

Are you seeing python exceptions? Most of them will return HTTP error codes which indicate infrastructure issues, usually network connection or authentication issues.

## Support
This tool is not officially supported by NetApp and provided as-is.
Run at your own risk.

### Risk assessment

It only does read operations, except when growing a volume. To grow a volume, it queries a volume, changes the *quotaInBytes* parameter and does an PUT API call, which is the equivalent of changing the size in the UI.

Larger volumes incur more monthly costs.

### Getting help
If you have any issues, please check logs first. When ran as Cloud Function, it will log a line for each volume to Cloud Logging. Any indication why it fails? Make sure the pass the parameters correctly.

Next check the issue section of this repository to see if it is a known issue.

If you still found no resolution, please consider opening an GitHub issue for this repository. Support is best-effort.
