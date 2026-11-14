{% macro query_comment(node) %}
    {%- set job_labels = {
        'application': 'dbt',
        'edition': 'bigquery',
        'target': target.name,
        'invocation': invocation_id
    } -%}
    {% do return(tojson(job_labels)) %}
{% endmacro %}
