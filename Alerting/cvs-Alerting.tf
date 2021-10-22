# Using Google Cloud Monitoring to monitor capacity for CVS volumes
# 
# Create a alert policy which monitors usage against an threshold
# If threshold is passed, an incident is sent to the configured
# notification channels
#
# usage_ratio = volume_used / volume_allocated
# See https://cloud.google.com/architecture/partners/netapp-cloud-volumes/monitoring?hl=en_US
# for list of available metrics
#

variable "gcp_project" {
    type = string
    description = "GCP ProjectID"
    default = null
}

provider "google" {
    project = var.gcp_project
}

# Fetch project number
data "google_project" "project" {
}

# Create topic
# Used for Incidents sent from Cloud Monitorint alert policy
# to resize script running as Cloud Function
resource "google_pubsub_topic" "CVSCapacityManagerEvents" {
  name = "CVSCapacityManagerEvents"
}

# Grant "Monitoring Notification Service Agent" permissions to publish to PubSub topic
# see https://cloud.google.com/monitoring/support/notification-options#pubsub
resource "google_pubsub_topic_iam_binding" "MNSA_binding" {
  project = google_pubsub_topic.CVSCapacityManagerEvents.project
  topic = google_pubsub_topic.CVSCapacityManagerEvents.name
  role = "roles/pubsub.publisher"
  members = ["serviceAccount:service-${data.google_project.project.number}@gcp-sa-monitoring-notification.iam.gserviceaccount.com"]
}

# Create Cloud Monitoring notification channel
resource "google_monitoring_notification_channel" "cvs-channel" {
  display_name = "CVS SpaceRunningLow Alerts"
  type         = "pubsub"
  labels = {
    topic = google_pubsub_topic.CVSCapacityManagerEvents.id
  }
}

# Create CVS Alert policy
resource "google_monitoring_alert_policy" "alert_policy" {
    display_name = "CVS-SpaceRunningLow"
    combiner     = "OR"

    # TODO: Set threshold here (default = 80%)
    # change "val() > 0.8" to match your preferred threshold (0 = 0%, 0.8 = 80%, 1 = 100%)
    # Note: snapReserve users, please see
    # https://cloud.google.com/architecture/partners/netapp-cloud-volumes/monitoring?hl=en_US
    conditions {
        display_name = "Volume usage threshold"
        condition_monitoring_query_language {
            query = <<EOF
fetch cloudvolumesgcp-api.netapp.com/CloudVolume
| {
metric 'cloudvolumesgcp-api.netapp.com/cloudvolume/volume_usage' | filter (metric.type == 'logical') 
;
metric 'cloudvolumesgcp-api.netapp.com/cloudvolume/volume_size'
} | join | div
| group_by sliding(5m), max(val())
| condition val() > 0.8
EOF
            duration = "0s"
        }
    }

    # Whom to notify
    # See https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/monitoring_notification_channel
    # and https://registry.terraform.io/providers/hashicorp/google/latest/docs/data-sources/monitoring_notification_channel
    notification_channels = [google_monitoring_notification_channel.cvs-channel.name]

    documentation {
        content = "Usage of CVS volume exceeded threshold. Increase volume allocation to avoid out-of-space conditions."
    }
}
