"""Admission responsibilities behind the legacy queue-admission facade.

Modules own request launching, holds, wait resolution, review checks, ordering,
verdicts and service lifetime. Cross-responsibility calls use their owning
module so compatibility patches reach the same objects the service calls.
"""
