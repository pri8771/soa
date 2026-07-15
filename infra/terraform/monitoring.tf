# Telemetry / alerting scaffolding (REL-002, feeding REL-007). An uptime
# check on the API and one example alert policy wired to the owner's
# notification channels. The full alert catalog lives in
# docs/ALERTS.md; this is the platform hook those alerts attach to.

resource "google_monitoring_uptime_check_config" "api" {
  display_name = "${local.name_prefix}-api-uptime"
  timeout      = "10s"

  http_check {
    path         = "/health/live"
    port         = 443
    use_ssl      = true
    validate_ssl = true
  }

  monitored_resource {
    type = "uptime_url"
    labels = {
      project_id = var.project_id
      # Prefer a custom hostname when one exists; otherwise derive the
      # managed Cloud Run hostname directly from the created service.
      host = coalesce(
        var.api_uptime_host,
        trimprefix(google_cloud_run_v2_service.api.uri, "https://"),
      )
    }
  }

  depends_on = [google_project_service.required]
}

# Example: alert when the API uptime check fails. Real thresholds/owners
# come from docs/ALERTS.md (REL-007); this shows the wiring.
resource "google_monitoring_alert_policy" "api_down" {
  count        = length(var.alert_notification_channels) > 0 ? 1 : 0
  display_name = "${local.name_prefix}-api-unreachable"
  combiner     = "OR"

  conditions {
    display_name = "API uptime check failing"
    condition_threshold {
      filter          = "metric.type=\"monitoring.googleapis.com/uptime_check/check_passed\" AND resource.type=\"uptime_url\""
      comparison      = "COMPARISON_LT"
      threshold_value = 1
      duration        = "300s"
      aggregations {
        alignment_period   = "300s"
        per_series_aligner = "ALIGN_FRACTION_TRUE"
      }
    }
  }

  notification_channels = var.alert_notification_channels
}
