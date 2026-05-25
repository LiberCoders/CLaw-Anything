# Claw-Anything Examples

This directory ships **minimal, self-contained samples** so you can run the framework end-to-end and use them as templates for your own work. None of the data here is copied from the original benchmark; everything uses fictional placeholder identities (`Acme Cloud`, `alex.doe@example.com`, etc.).

## Layout

```
examples/
├── personas/
│   └── demo_engineer.yaml           # 1 generic persona (input to build-persona)
├── seed_tasks/
│   ├── S001_calendar_conflict.yaml  # generic decision task
│   ├── S002_email_triage.yaml       # generic info-triage task
│   └── S003_doc_summarize.yaml      # generic summarization task
└── ready_to_run/
    └── T001_demo/                   # a complete, runnable eval task
        ├── task.yaml                # 2 mock services (gmail + calendar)
        ├── grader.py                # rule-based grader
        └── fixtures/                # tiny seeded data
            ├── gmail/inbox.json
            └── calendar/events.json
```

## Use case 1: Run the demo eval task

```bash
# Once: build the sandbox image and copy the config template
claw-anything build-image
cp config.example.yaml config.yaml
# Edit config.yaml to set your model + API key

# Run a single trial
claw-anything run \
  --task examples/ready_to_run/T001_demo \
  --config config.yaml

# Or batch-run with N trials in parallel (sandboxed)
claw-anything batch \
  --tasks-dir examples/ready_to_run \
  --config config.yaml \
  --sandbox \
  --trials 3 \
  --parallel 2
```

## Use case 2: Build a persona's gold environment from the seed tasks

```bash
claw-anything build-persona \
  --persona examples/personas/demo_engineer.yaml \
  --seed-tasks examples/seed_tasks \
  --rounds 3 \
  --output ./out/demo_engineer_env \
  --config config.yaml
```

This iteratively adapts the seed tasks to the persona, generating fixtures and activity logs in `./out/demo_engineer_env/`.

## Use case 3: Generate evaluation tasks from a built persona environment

```bash
claw-anything gen-eval \
  --env ./out/demo_engineer_env \
  --seed-tasks examples/seed_tasks \
  --output ./out/demo_eval_tasks \
  --max-tasks 3 \
  --config config.yaml
```

## Use case 4: Author your own task

Use `template/task_template.yaml` and `template/grader_template.py` as the starting point for hand-written tasks.
