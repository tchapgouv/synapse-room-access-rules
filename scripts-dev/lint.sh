#!/usr/bin/env bash
# Runs linting scripts and type checking
# ruff - formats, lints, finds mistakes, and sorts import statements
# mypy - checks type annotations

set -e

files=(
  "room_access_rules"
  "tests"
)

# Print out the commands being run
set -x

ruff format
ruff check --fix
mypy room_access_rules
