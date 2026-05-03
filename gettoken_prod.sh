#!/bin/bash
RESPONSE=$(curl -s -X POST https://apolodigital-inventario-production.up.railway.app/api/v1/auth/login \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "username=admin@apolodigital.lat&d0sgvLlUGEKAYmfcJyH5=$1")
echo "$RESPONSE" | grep -o '"eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIzYjc1MWU4YS1mNTVmLTQzN2UtYmQ5ZC1kYWFhM2YxNTgzODIiLCJlbWFpbCI6ImFkbWluQGFwb2xvZGlnaXRhbC5sYXQiLCJuZWdvY2lvX2lkIjoiNDcyOWQ4MDEtYjEyNy00NjkzLWIxNGUtODAwYTk1YmJiYTA1Iiwicm9sIjoic3VwZXJhZG1pbiIsImV4cCI6MTc3Nzc4Mjg0NH0.VmUoOeD7OsgNG9XZePMATiGIh-BSzLgRSAfxpN1jvlQ":"[^"]*"' | cut -d'"' -f4
