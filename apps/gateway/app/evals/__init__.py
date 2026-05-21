"""Day 5 eval harness.

Runs the agent against a fixed golden set of caregiver transcripts and scores
the trajectory + outcome. Surfaces regressions (lost tool calls, hallucinated
ids, missed flags) and lets us compare planner models side-by-side.
"""
