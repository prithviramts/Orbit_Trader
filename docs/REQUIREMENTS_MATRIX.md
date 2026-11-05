# Requirements → evidence matrix

Evidence types: **T** = automated test (file::name), **UI** = screen inspected in the in-app browser on
2026-11-05, **B** = blocked/external, **D** = documented only.

| Requirement | Evidence | Status |
|---|---|---|
| No activation on first launch, refresh or failed onboarding | T engine::test_first_launch_is_observe_and_never_submits, ::test_activation_refused_before_checks, ::test_activation_refused_without_credentials, api::test_activation_requires_password_and_checks, ::test_get_requests_never_change_state | Test passed |
| Live broker URL rejected; paper enforced | T engine::test_live_or_unknown_broker_urls_rejected, api::test_live_base_url_refuses_startup | Test passed |
| Secret redaction / masking | T engine::test_secret_redaction, api::test_ops_config_masks_secrets; test.ps1 secret scan | Test passed |
| USD/AED 3.6725 incl. losses & rounding | T core::test_usd_aed_reference_conversion (15 cases), ::test_exact_reference_values_and_direction | Test passed |
| Hand-checked FIFO fixture (USD 1,000 → 1,018 / AED 3,738.61) | T core::test_hand_calculated_fifo_fixture (arithmetic independently re-derived) | Test passed |
| SMA warm-up, equality, transitions, next session | T core::test_warmup_…, ::test_equality_…, ::test_strictly_below_goes_cash, ::test_backtest_next_session_timing_and_sizing | Test passed |
| Duplicate session decisions | T engine::test_duplicate_evaluation_attempts_produce_one_decision | Test passed |
| Missing/stale bars, no future prices, no substitution | T core::test_no_future_prices_used, ::test_missing_current_bar_never_substituted, ::test_missing_bars_reported_not_filled, engine::test_missing_current_bar_waits_then_skips | Test passed |
| Split handling / adjusted consistency | T core::test_split_raw_matches_adjusted, ::test_split_keeps_basis | Test passed |
| Insufficient cash after gap, rounding, no short/borrow | T engine::test_insufficient_cash_after_gap_reduces_notional, core::test_backtest_gap_reduces_entry_and_never_borrows, ::test_ledger_blocks_borrowing_and_shorting, ::test_floor_qty_never_rounds_up, engine::test_execution_guard_blocks_bypass_attempts | Test passed |
| Accepted-then-timeout, restart after submission | T engine::test_timeout_after_accept_…, ::test_timeout_and_not_found_stays_ambiguous_…, ::test_restart_after_submission_…, ::test_restart_with_submitting_and_no_broker_order_goes_ambiguous | Test passed |
| Duplicate / out-of-order fills | T engine::test_duplicate_and_out_of_order_fill_snapshots | Test passed |
| Partial fill then cancel (entry); residual exit (exit) | T engine::test_partial_entry_then_cancel_…, ::test_partial_exit_creates_residual_exit_with_lineage | Test passed |
| Manual account drift / foreign orders → pause | T engine::test_manual_account_drift_pauses_…, ::test_foreign_order_pauses_after_confirmation | Test passed |
| Holidays, DST, early close, missed window, overlapping workers | T core::test_calendar_holidays_early_close_and_dst, engine::test_holiday_and_early_close_scheduling, ::test_dst_window_uses_calendar_open, ::test_missed_window_requires_review_and_no_catchup, ::test_overlapping_workers_cannot_both_hold_the_lease | Test passed |
| Conflicting order cancelled before reassessment | T engine::test_conflicting_working_order_is_cancelled_before_reassessment | Test passed |
| Rejection visible | T engine::test_rejection_is_visible_failure | Test passed |
| Benchmarks parity, costs, price-only, drawdown, no annualising short runs | T core::test_benchmarks_share_dates_capital_and_costs, ::test_short_runs_do_not_annualize, ::test_risk_pause_blocks_buys_in_backtest | Test passed |
| Cash flows not counted as profit | T engine::test_deposit_invalidates_performance_and_pauses_entries, ::test_review_cash_activity_failure_is_not_fatal | Test passed |
| Drawdown overlay: warn 5%, latch 10%, override versioned, exits continue | T core::test_drawdown_warn_and_latch_never_self_resets, engine::test_drawdown_warning_has_hysteresis, ::test_live_drawdown_latches_entries_and_override_is_respected, ::test_risk_pause_blocks_entries_but_not_exits, ::test_risk_pause_still_allows_scheduled_exit | Test passed |
| Settings versioning; pending intents reviewed | T engine::test_settings_change_moves_pending_intent_to_review, ::test_review_settings_change_does_not_block_manual_close | Test passed |
| Close position never oversells | T engine::test_close_position_never_oversells, ::test_review_manual_close_pauses_rebuy | Test passed |
| SAMPLE/CSV never orders; SAMPLE labelled | T engine::test_sample_or_csv_data_never_creates_orders, core::test_sample_series_is_labelled_and_deterministic, api::test_sample_backtest_end_to_end_via_api; UI Research/Market SAMPLE banners | Test passed + UI |
| Failed notifications independent; redacted/deduped push | T engine::test_notification_failure_never_retries_trades, ::test_public_push_is_redacted_and_deduplicated | Test passed |
| Auth, CSRF, cross-origin, throttling, logout, cookie flags, Host | T api::test_unauthenticated_access_denied, ::test_no_default_password, ::test_csrf_required_…, ::test_cross_origin_post_blocked_…, ::test_bad_host_header_rejected, ::test_login_throttling_…, ::test_logout_invalidates_session, ::test_session_cookie_flags | Test passed |
| Stored script payloads, oversized imports, hostile news, notification link | T api::test_stored_script_payloads_are_data_only, ::test_oversized_and_invalid_imports_rejected, ::test_hostile_news_instruction_cannot_trade, ::test_notification_link_cannot_authorize | Test passed |
| Remote bind refused without HTTPS config; paused state persists across restart | T api::test_remote_bind_refused_without_https, ::test_paused_state_persists_across_restart | Test passed |
| UI: login, overview, SAMPLE replay + benchmarks, Learn predict/reveal, pause + surviving positions, decision → order → fill timeline (MOCK), phone width | UI inspected (in-app browser) | Inspected manually; **no automated browser test suite** ⚠ |
| Graceful worker stop + lease release | Manual run 2026-11-05: stop flag → "Worker stopped cleanly", lease status `stopped` | Verified manually |
| Backup / restore drill | `orbit.cli restore-drill` (see RELEASE_CHECKLIST) | Verified on demo DB |
| Alpaca read path (account, positions, orders, asset, clock, bars) | Read-only check on the owner paper account 2026-11-05; tests/test_alpaca_adapter.py | Externally verified (read path) |
| Supervised paper order at Alpaca | Mocks only | **Blocked: requires owner activation** |
| ntfy phone receipt | Adapter + failure tests | **Blocked: owner must configure and confirm** |
| Container build / Linux run | Files written; `bash -n` and YAML parse checks | **Blocked: Docker not installed here** |
| Remote mode (HTTPS origin, Secure cookie, Host checks) | T api::test_remote_mode_accepts_only_public_https_origin | Test passed; untested behind real `tailscale serve` |
| External heartbeat (dead-man's switch) | T heartbeat::test_pings_and_rate_limits, ::test_failures_never_raise, ::test_http_url_refused_…, ::test_heartbeat_url_is_redacted | Test passed |
