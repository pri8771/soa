# Cloud Storage bucket for document artifacts (REL-002). Uniform
# bucket-level access (no per-object ACLs), versioning off (artifacts are
# immutable rows over immutable keys — see the S3/GCS adapter notes), and
# public access prevented. Retention/deletion is driven by the
# application (SEC-008/010), not bucket lifecycle, so it stays authorized
# and audited.

resource "google_storage_bucket" "artifacts" {
  name                        = "${var.project_id}-${local.name_prefix}-artifacts"
  location                    = var.storage_location
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = !var.deletion_protection
  labels                      = local.labels

  versioning {
    enabled = false
  }

  # Browser clients upload directly to short-lived signed URLs and fetch
  # signed page/download URLs from the Firebase origin. Without an explicit
  # bucket CORS policy those otherwise-valid requests fail at preflight.
  # The checksum metadata header is signed and required on every upload.
  cors {
    origin = var.api_cors_allowed_origins
    method = ["GET", "HEAD", "PUT"]
    response_header = [
      "Content-Type",
      "ETag",
      "x-goog-content-length-range",
      "x-goog-meta-sha256",
    ]
    max_age_seconds = 3600
  }

  # No lifecycle expiration: real object deletion goes through the
  # application (SEC-008/010) so it is authorized and audited. GCS aborts
  # abandoned resumable uploads on its own.

  depends_on = [google_project_service.required]
}
