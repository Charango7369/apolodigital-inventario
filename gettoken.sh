#!/bin/bash
RESPONSE=$(curl -s -X POST http://127.0.0.1:8001/api/v1/auth/login \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "username=admin@apolodigital.lat&password=$1")
echo "$RESPONSE" | grep -o '"access_token":"[^"]*"' | cut -d'"' -f4
