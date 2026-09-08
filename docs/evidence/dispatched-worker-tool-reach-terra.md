# Dispatched-worker IMAS data-dictionary tool reach

## Outcome

**Server answered and returned a version range.**

This evidence was produced inside this dispatched Codex worker process. The
runtime identifies this worker as a GPT-5 Codex agent; its exposed runtime
metadata provides no more-specific model identifier, so this report does not
infer one from the dispatch-node name.

Harness evidence: the MCP tool catalogue was enumerated directly from this
process's tool surface. It contained **97** MCP tools. The tool call
required no approval prompt, and no approval was requested.

## Data-dictionary call

- Server namespace: `imas_dd`
- Tool called: `mcp__imas_dd__get_dd_versions`
- Call result: success (`isError: false`)
- Earliest version: `3.22.0`
- Latest version: `4.1.1`

The earliest and latest values above were read exclusively from the following
tool response, not from local files, configuration, memory, or a network
fallback:

```text
DD Version Metadata
Current version: 4.1.0
Version range: 3.22.0 - 4.1.1
Version count: 35
Version chain: 3.22.0 -> 3.23.0 -> 3.23.1 -> 3.23.2 -> 3.23.3 -> 3.24.0 -> 3.25.0 -> 3.26.0 -> 3.27.0 -> 3.28.0 -> 3.28.1 -> 3.29.0 -> 3.30.0 -> 3.31.0 -> 3.32.0 -> 3.32.1 -> 3.33.0 -> 3.34.0 -> 3.35.0 -> 3.36.0 -> 3.37.0 -> 3.37.1 -> 3.37.2 -> 3.38.0 -> 3.38.1 -> 3.39.0 -> 3.40.0 -> 3.40.1 -> 3.41.0 -> 3.42.0 -> 3.42.1 -> 3.42.2 -> 4.0.0 -> 4.1.0 -> 4.1.1
```

## Visible MCP tools

Count: **97**

- `mcp__codex_apps__codex_document_control_execute_document_command`
- `mcp__codex_apps__codex_document_control_get_document_tool_schemas`
- `mcp__codex_apps__codex_document_control_list_document_sessions`
- `mcp__codex_apps__hotline_get_local_hotline`
- `mcp__codex_apps__plugin_management_get_app_permissions`
- `mcp__codex_apps__plugin_management_get_plugin_dependencies`
- `mcp__codex_apps__plugin_management_uninstall_app`
- `mcp__codex_apps__plugin_management_update_app_permissions`
- `mcp__codex_apps__safety_settings_get_family_info`
- `mcp__codex_apps__safety_settings_get_parental_controls`
- `mcp__codex_apps__safety_settings_get_trusted_contact`
- `mcp__codex_apps__safety_settings_prepare_parental_control_update`
- `mcp__codex_apps__safety_settings_update_parental_control`
- `mcp__codex_apps__sites_add_custom_domain`
- `mcp__codex_apps__sites_change_site_slug`
- `mcp__codex_apps__sites_create_site`
- `mcp__codex_apps__sites_create_source_repository_write_credential`
- `mcp__codex_apps__sites_deploy_private_site_version`
- `mcp__codex_apps__sites_deploy_site_version`
- `mcp__codex_apps__sites_generate_siwc_bypass_token`
- `mcp__codex_apps__sites_get_deployment_status`
- `mcp__codex_apps__sites_get_environment_variables`
- `mcp__codex_apps__sites_get_site`
- `mcp__codex_apps__sites_get_site_version`
- `mcp__codex_apps__sites_get_site_worker_logs`
- `mcp__codex_apps__sites_list_custom_domains`
- `mcp__codex_apps__sites_list_site_versions`
- `mcp__codex_apps__sites_list_sites`
- `mcp__codex_apps__sites_read_database_overview`
- `mcp__codex_apps__sites_read_database_table_rows`
- `mcp__codex_apps__sites_refresh_custom_domain_status`
- `mcp__codex_apps__sites_remove_custom_domain`
- `mcp__codex_apps__sites_save_site_version`
- `mcp__codex_apps__sites_update_environment_variables`
- `mcp__codex_apps__sites_update_site_access`
- `mcp__codex_apps__sites_update_site_metadata`
- `mcp__imas_cx__add_to_graph`
- `mcp__imas_cx__check_dd_paths`
- `mcp__imas_cx__check_standard_names`
- `mcp__imas_cx__edit_standard_name`
- `mcp__imas_cx__fetch_content`
- `mcp__imas_cx__fetch_dd_error_fields`
- `mcp__imas_cx__fetch_dd_paths`
- `mcp__imas_cx__fetch_standard_names`
- `mcp__imas_cx__find_related_dd_paths`
- `mcp__imas_cx__find_related_standard_names`
- `mcp__imas_cx__get_dd_catalog`
- `mcp__imas_cx__get_dd_changelog`
- `mcp__imas_cx__get_dd_cocos_fields`
- `mcp__imas_cx__get_dd_identifiers`
- `mcp__imas_cx__get_dd_migration_guide`
- `mcp__imas_cx__get_dd_version_context`
- `mcp__imas_cx__get_dd_versions`
- `mcp__imas_cx__get_facility_coverage`
- `mcp__imas_cx__get_graph_schema`
- `mcp__imas_cx__get_ids_summary`
- `mcp__imas_cx__get_logs`
- `mcp__imas_cx__get_standard_name_summary`
- `mcp__imas_cx__list_dd_paths`
- `mcp__imas_cx__list_grammar_vocabulary`
- `mcp__imas_cx__list_logs`
- `mcp__imas_cx__list_promotion_candidates`
- `mcp__imas_cx__list_standard_names`
- `mcp__imas_cx__repl`
- `mcp__imas_cx__search_code`
- `mcp__imas_cx__search_dd_clusters`
- `mcp__imas_cx__search_dd_paths`
- `mcp__imas_cx__search_docs`
- `mcp__imas_cx__search_signals`
- `mcp__imas_cx__search_standard_names`
- `mcp__imas_cx__signal_analytics`
- `mcp__imas_cx__tail_logs`
- `mcp__imas_cx__trace_standard_name_provenance`
- `mcp__imas_cx__update_facility_config`
- `mcp__imas_dd__check_dd_paths`
- `mcp__imas_dd__fetch_dd_error_fields`
- `mcp__imas_dd__fetch_dd_paths`
- `mcp__imas_dd__fetch_standard_names`
- `mcp__imas_dd__find_related_dd_paths`
- `mcp__imas_dd__get_dd_catalog`
- `mcp__imas_dd__get_dd_changelog`
- `mcp__imas_dd__get_dd_cocos_fields`
- `mcp__imas_dd__get_dd_identifiers`
- `mcp__imas_dd__get_dd_migration_guide`
- `mcp__imas_dd__get_dd_version_context`
- `mcp__imas_dd__get_dd_versions`
- `mcp__imas_dd__get_ids_summary`
- `mcp__imas_dd__list_dd_paths`
- `mcp__imas_dd__list_standard_names`
- `mcp__imas_dd__search_dd_clusters`
- `mcp__imas_dd__search_dd_paths`
- `mcp__imas_dd__search_standard_names`
- `mcp__reckon_audit`
- `mcp__reckon_crew`
- `mcp__reckon_edit_plan`
- `mcp__reckon_read_plan`
- `mcp__reckon_roadmap`

