# Project name used for resource naming
project_name = "travel-planner"

# Your Production Google Cloud project id
prod_project_id = "agentspace-452714"

# Your Staging / Test Google Cloud project id
staging_project_id = "agentspace-452714"

# Your Google Cloud project ID that will be used to host the Cloud Build pipelines.
cicd_runner_project_id = "agentspace-452714"
# Name of the host connection you created in Cloud Build
host_connection_name = "git-travel-planner"
github_pat_secret_id = "travel-planner-github-pat"

repository_owner = "caugusto"

# Name of the repository you added to Cloud Build
repository_name = "travel-planner"

# The Google Cloud region you will use to deploy the infrastructure
region = "us-central1"
