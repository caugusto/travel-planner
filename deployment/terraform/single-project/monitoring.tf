# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Operational monitoring built on the agent's structured JSON logs
# (see app/observability.py). Each log line carries jsonPayload.event.

locals {
  agent_log_filter = "jsonPayload.logger=\"travel_planner\""
}

resource "google_logging_metric" "tool_errors" {
  project     = var.project_id
  name        = "${var.project_name}/tool_errors"
  description = "Agent tool calls that returned status=error or raised."
  filter      = "${local.agent_log_filter} AND (jsonPayload.event=\"tool_exception\" OR (jsonPayload.event=\"tool_end\" AND jsonPayload.status=\"error\"))"
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    labels {
      key        = "tool"
      value_type = "STRING"
    }
  }
  label_extractors = {
    tool = "EXTRACT(jsonPayload.tool)"
  }
}

resource "google_logging_metric" "tool_latency" {
  project         = var.project_id
  name            = "${var.project_name}/tool_latency_ms"
  description     = "Agent tool latency distribution (ms)."
  filter          = "${local.agent_log_filter} AND jsonPayload.event=\"tool_end\""
  value_extractor = "EXTRACT(jsonPayload.duration_ms)"
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "DISTRIBUTION"
    unit        = "ms"
    labels {
      key        = "tool"
      value_type = "STRING"
    }
  }
  label_extractors = {
    tool = "EXTRACT(jsonPayload.tool)"
  }
  bucket_options {
    exponential_buckets {
      num_finite_buckets = 20
      growth_factor      = 2
      scale              = 5
    }
  }
}

resource "google_logging_metric" "guardrail_blocks" {
  project     = var.project_id
  name        = "${var.project_name}/guardrail_blocks"
  description = "Requests blocked by safety guardrails or policy gates."
  filter      = "${local.agent_log_filter} AND (jsonPayload.event=\"guardrail_block\" OR jsonPayload.event=\"policy_block\")"
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
  }
}

resource "google_logging_metric" "llm_tokens" {
  project         = var.project_id
  name            = "${var.project_name}/llm_output_tokens"
  description     = "Output tokens per LLM call, by agent."
  filter          = "${local.agent_log_filter} AND jsonPayload.event=\"llm_usage\""
  value_extractor = "EXTRACT(jsonPayload.output_tokens)"
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "DISTRIBUTION"
    labels {
      key        = "agent"
      value_type = "STRING"
    }
  }
  label_extractors = {
    agent = "EXTRACT(jsonPayload.agent)"
  }
  bucket_options {
    exponential_buckets {
      num_finite_buckets = 16
      growth_factor      = 2
      scale              = 16
    }
  }
}

resource "google_monitoring_alert_policy" "tool_error_spike" {
  project      = var.project_id
  display_name = "${var.project_name}: tool error spike"
  combiner     = "OR"
  conditions {
    display_name = "More than 10 tool errors in 5 minutes"
    condition_threshold {
      filter          = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.tool_errors.name}\" AND resource.type=\"aiplatform.googleapis.com/ReasoningEngine\""
      comparison      = "COMPARISON_GT"
      threshold_value = 10
      duration        = "0s"
      aggregations {
        alignment_period   = "300s"
        per_series_aligner = "ALIGN_SUM"
      }
    }
  }
  documentation {
    content   = "An external API (weather, holidays, FX) may be down. Check Cloud Trace spans named http.get and the tool_end logs."
    mime_type = "text/markdown"
  }
}

resource "google_monitoring_dashboard" "agent" {
  project = var.project_id
  dashboard_json = jsonencode({
    displayName = "${var.project_name} - agent health"
    mosaicLayout = {
      columns = 12
      tiles = [
        {
          width = 6, height = 4,
          widget = {
            title = "Tool errors by tool"
            xyChart = { dataSets = [{ timeSeriesQuery = { timeSeriesFilter = {
              filter      = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.tool_errors.name}\""
              aggregation = { alignmentPeriod = "60s", perSeriesAligner = "ALIGN_SUM", groupByFields = ["metric.label.tool"], crossSeriesReducer = "REDUCE_SUM" }
            } } }] }
          }
        },
        {
          xPos = 6, width = 6, height = 4,
          widget = {
            title = "Tool latency p95 (ms)"
            xyChart = { dataSets = [{ timeSeriesQuery = { timeSeriesFilter = {
              filter      = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.tool_latency.name}\""
              aggregation = { alignmentPeriod = "60s", perSeriesAligner = "ALIGN_PERCENTILE_95", groupByFields = ["metric.label.tool"], crossSeriesReducer = "REDUCE_MAX" }
            } } }] }
          }
        },
        {
          yPos = 4, width = 6, height = 4,
          widget = {
            title = "Output tokens by agent (p50)"
            xyChart = { dataSets = [{ timeSeriesQuery = { timeSeriesFilter = {
              filter      = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.llm_tokens.name}\""
              aggregation = { alignmentPeriod = "60s", perSeriesAligner = "ALIGN_PERCENTILE_50", groupByFields = ["metric.label.agent"], crossSeriesReducer = "REDUCE_MAX" }
            } } }] }
          }
        },
        {
          xPos = 6, yPos = 4, width = 6, height = 4,
          widget = {
            title = "Guardrail / policy blocks"
            xyChart = { dataSets = [{ timeSeriesQuery = { timeSeriesFilter = {
              filter      = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.guardrail_blocks.name}\""
              aggregation = { alignmentPeriod = "300s", perSeriesAligner = "ALIGN_SUM", crossSeriesReducer = "REDUCE_SUM" }
            } } }] }
          }
        }
      ]
    }
  })
}
