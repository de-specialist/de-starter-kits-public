# Documentation site

Build documentation locally with `bash scripts/build_docs_site.sh` using Python
3.11. The script creates separate locked environments, stages Markdown, generates
credential-free dbt documentation and runs a strict MkDocs build and link checks.
Set `DBT_DOCS=skip` to use placeholder dbt catalogs during Markdown-only changes.

Public CI builds documentation but does not deploy it. Configure hosting in your
own private deployment repository. Do not publish manifests generated from real
warehouse profiles or customer schemas. The visibility checker remains a backstop
for accidentally introduced private front matter; ignoring a page in the website
never makes a tracked source file private.
