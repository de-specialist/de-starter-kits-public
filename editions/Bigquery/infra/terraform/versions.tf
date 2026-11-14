terraform {
  required_version = ">= 1.9.0"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "= 7.46.2"
    }
    github = {
      source  = "integrations/github"
      version = "= 6.13.0"
    }
  }
}

provider "google" {
  project = var.project_id
}

provider "github" {
  owner = var.github_owner
  # Authentication comes only from the GITHUB_TOKEN environment variable.
}
