#!/bin/sh
# pre-commit hook: refuse files that must never reach git (CLAUDE.md hard rules + plan 12 secrets).
status=0
for f in "$@"; do
  case "$f" in
    .env.example|*/.env.example) continue ;;
    .env|.env.*|*/.env|*/.env.*)
      echo "blocked: $f (.env files hold secrets)"; status=1; continue ;;
    docs/materiais/*)
      echo "blocked: $f (client material is data, never committed)"; status=1; continue ;;
    docs/privado/*|docker/tenants/*|docker/materiais/*|knowledge_inventory.yaml|*/knowledge_inventory.yaml)
      echo "blocked: $f (private: stays out of this public repository)"; status=1; continue ;;
    docker/secrets/*|docker/prod.env|docker/.deploy.env)
      echo "blocked: $f (deployment secrets)"; status=1; continue ;;
    *.pdf|*.PDF)
      echo "blocked: $f (PDFs are client material; keep them out of git)"; status=1; continue ;;
  esac
  # a local list of what identifies a client or the owner (ADR-023). Reports the file only: the list itself is private.
  if [ -f docs/privado/termos.txt ] && [ -f "$f" ]; then
    LC_ALL=C tr 'A-Z' 'a-z' < "$f" 2>/dev/null | LC_ALL=C grep -qFf docs/privado/termos.txt 2>/dev/null      # the list is in lowercase
    rc=$?
    if [ "$rc" -eq 0 ]; then
      echo "blocked: $f mentions something on the private list (docs/privado/termos.txt)"; status=1
    elif [ "$rc" -gt 1 ]; then                       # a check that could not run does not count as clean
      echo "blocked: $f could not be checked against the private list"; status=1
    fi
  fi
  # provider keys / Meta tokens pasted into a tracked file: report file:line only, never the matched value
  case "$f" in tests/unit/test_security.py) continue ;; esac
  hits=$(grep -nE 'sk-(proj|or-v1)-[A-Za-z0-9_-]{20,}|EAA[A-Za-z0-9]{60,}' -- "$f" 2>/dev/null | cut -d: -f1)
  if [ -n "$hits" ]; then
    echo "blocked: $f line(s) $(echo "$hits" | tr '\n' ' ')look like an OpenAI/OpenRouter key or a Meta token"; status=1
  fi
done
exit $status
