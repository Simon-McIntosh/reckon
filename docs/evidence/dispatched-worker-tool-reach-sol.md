# Dispatched worker MCP tool reach: Sol

## Runtime identity

- Harness: Codex CLI (`launch: cli`, `backend: codex`)
- Model: `gpt-5.6-sol` (runtime alias `sol5.6`)
- Sandbox: `worktree-full`
- Runtime identity source: this worker read its own live run record from inside the dispatched process using `mcp__reckon_crew`; these values were not inferred from the dispatch brief.

## Outcome

**Outcome 1 of 4: the server answered and this worker has a version range.**

The discovered and called tool was exactly `mcp__imas_dd__get_dd_versions`. The call completed with `isError: false`; it did not request approval, and no approval was required. The figures below come only from that named tool call. From the response, the earliest available version is `3.22.0`, the latest available version is `4.1.1`, the current version is `4.1.0`, and the ordered set contains 35 versions.

The other three mutually exclusive outcomes did not occur: the reachable server did not refuse or error; the tools were not absent; and no other condition prevented the attempt.

### Verbatim tool response

```text
DD Version Metadata
Current version: 4.1.0
Version range: 3.22.0 - 4.1.1
Version count: 35
Version chain: 3.22.0 -> 3.23.0 -> 3.23.1 -> 3.23.2 -> 3.23.3 -> 3.24.0 -> 3.25.0 -> 3.26.0 -> 3.27.0 -> 3.28.0 -> 3.28.1 -> 3.29.0 -> 3.30.0 -> 3.31.0 -> 3.32.0 -> 3.32.1 -> 3.33.0 -> 3.34.0 -> 3.35.0 -> 3.36.0 -> 3.37.0 -> 3.37.1 -> 3.37.2 -> 3.38.0 -> 3.38.1 -> 3.39.0 -> 3.40.0 -> 3.40.1 -> 3.41.0 -> 3.42.0 -> 3.42.1 -> 3.42.2 -> 4.0.0 -> 4.1.0 -> 4.1.1
```

The tool wrapper also returned `isError: false` outside the text content shown above.

## Why this lane needed its own check

The existing evidence in the live plan records a successful call from a `gpt-5.6-luna` dispatched worker. That settles its harness instance and lane only. Tool exposure can differ per dispatched model because the harness can provide a different tool set or truncate one, and a model can fail to invoke a tool it can see. This `gpt-5.6-sol` process therefore had to demonstrate its own tool visibility and call rather than inherit the Luna result.

## Visible MCP tool inventory

This process exposed **97 MCP tools** in its runtime tool registry. The complete list below has length 97 and preserves each name character for character:

1. `mcp__codex_apps__codex_document_control_execute_document_command`
2. `mcp__codex_apps__codex_document_control_get_document_tool_schemas`
3. `mcp__codex_apps__codex_document_control_list_document_sessions`
4. `mcp__codex_apps__hotline_get_local_hotline`
5. `mcp__codex_apps__plugin_management_get_app_permissions`
6. `mcp__codex_apps__plugin_management_get_plugin_dependencies`
7. `mcp__codex_apps__plugin_management_uninstall_app`
8. `mcp__codex_apps__plugin_management_update_app_permissions`
9. `mcp__codex_apps__safety_settings_get_family_info`
10. `mcp__codex_apps__safety_settings_get_parental_controls`
11. `mcp__codex_apps__safety_settings_get_trusted_contact`
12. `mcp__codex_apps__safety_settings_prepare_parental_control_update`
13. `mcp__codex_apps__safety_settings_update_parental_control`
14. `mcp__codex_apps__sites_add_custom_domain`
15. `mcp__codex_apps__sites_change_site_slug`
16. `mcp__codex_apps__sites_create_site`
17. `mcp__codex_apps__sites_create_source_repository_write_credential`
18. `mcp__codex_apps__sites_deploy_private_site_version`
19. `mcp__codex_apps__sites_deploy_site_version`
20. `mcp__codex_apps__sites_generate_siwc_bypass_token`
21. `mcp__codex_apps__sites_get_deployment_status`
22. `mcp__codex_apps__sites_get_environment_variables`
23. `mcp__codex_apps__sites_get_site`
24. `mcp__codex_apps__sites_get_site_version`
25. `mcp__codex_apps__sites_get_site_worker_logs`
26. `mcp__codex_apps__sites_list_custom_domains`
27. `mcp__codex_apps__sites_list_site_versions`
28. `mcp__codex_apps__sites_list_sites`
29. `mcp__codex_apps__sites_read_database_overview`
30. `mcp__codex_apps__sites_read_database_table_rows`
31. `mcp__codex_apps__sites_refresh_custom_domain_status`
32. `mcp__codex_apps__sites_remove_custom_domain`
33. `mcp__codex_apps__sites_save_site_version`
34. `mcp__codex_apps__sites_update_environment_variables`
35. `mcp__codex_apps__sites_update_site_access`
36. `mcp__codex_apps__sites_update_site_metadata`
37. `mcp__imas_cx__add_to_graph`
38. `mcp__imas_cx__check_dd_paths`
39. `mcp__imas_cx__check_standard_names`
40. `mcp__imas_cx__edit_standard_name`
41. `mcp__imas_cx__fetch_content`
42. `mcp__imas_cx__fetch_dd_error_fields`
43. `mcp__imas_cx__fetch_dd_paths`
44. `mcp__imas_cx__fetch_standard_names`
45. `mcp__imas_cx__find_related_dd_paths`
46. `mcp__imas_cx__find_related_standard_names`
47. `mcp__imas_cx__get_dd_catalog`
48. `mcp__imas_cx__get_dd_changelog`
49. `mcp__imas_cx__get_dd_cocos_fields`
50. `mcp__imas_cx__get_dd_identifiers`
51. `mcp__imas_cx__get_dd_migration_guide`
52. `mcp__imas_cx__get_dd_version_context`
53. `mcp__imas_cx__get_dd_versions`
54. `mcp__imas_cx__get_facility_coverage`
55. `mcp__imas_cx__get_graph_schema`
56. `mcp__imas_cx__get_ids_summary`
57. `mcp__imas_cx__get_logs`
58. `mcp__imas_cx__get_standard_name_summary`
59. `mcp__imas_cx__list_dd_paths`
60. `mcp__imas_cx__list_grammar_vocabulary`
61. `mcp__imas_cx__list_logs`
62. `mcp__imas_cx__list_promotion_candidates`
63. `mcp__imas_cx__list_standard_names`
64. `mcp__imas_cx__repl`
65. `mcp__imas_cx__search_code`
66. `mcp__imas_cx__search_dd_clusters`
67. `mcp__imas_cx__search_dd_paths`
68. `mcp__imas_cx__search_docs`
69. `mcp__imas_cx__search_signals`
70. `mcp__imas_cx__search_standard_names`
71. `mcp__imas_cx__signal_analytics`
72. `mcp__imas_cx__tail_logs`
73. `mcp__imas_cx__trace_standard_name_provenance`
74. `mcp__imas_cx__update_facility_config`
75. `mcp__imas_dd__check_dd_paths`
76. `mcp__imas_dd__fetch_dd_error_fields`
77. `mcp__imas_dd__fetch_dd_paths`
78. `mcp__imas_dd__fetch_standard_names`
79. `mcp__imas_dd__find_related_dd_paths`
80. `mcp__imas_dd__get_dd_catalog`
81. `mcp__imas_dd__get_dd_changelog`
82. `mcp__imas_dd__get_dd_cocos_fields`
83. `mcp__imas_dd__get_dd_identifiers`
84. `mcp__imas_dd__get_dd_migration_guide`
85. `mcp__imas_dd__get_dd_version_context`
86. `mcp__imas_dd__get_dd_versions`
87. `mcp__imas_dd__get_ids_summary`
88. `mcp__imas_dd__list_dd_paths`
89. `mcp__imas_dd__list_standard_names`
90. `mcp__imas_dd__search_dd_clusters`
91. `mcp__imas_dd__search_dd_paths`
92. `mcp__imas_dd__search_standard_names`
93. `mcp__reckon_audit`
94. `mcp__reckon_crew`
95. `mcp__reckon_edit_plan`
96. `mcp__reckon_read_plan`
97. `mcp__reckon_roadmap`

The inventory is non-empty and includes 18 names presented with the `mcp__imas_dd__` prefix. No MCP call in this check required an approval that the non-interactive worker could not provide.

## Anti-fabrication confirmation

The only data-dictionary version figures in this report are attributed to `mcp__imas_dd__get_dd_versions` and reproduced from that tool's response above. No repository file, environment variable, configuration file, web page, or remembered version knowledge was used to supply or supplement those figures.
