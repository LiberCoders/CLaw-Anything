# Full-Environment Task Template

This directory contains all templates and schema definitions needed to create full-environment tasks.

## Directory Structure

```
template/
├── README.md                    # This file: overview
├── task_template.yaml           # task.yaml template (services + tools + endpoints — fixed sections)
├── grader_template.py           # grader.py template (grader skeleton)
└── fixture_schemas.yaml         # Fixture JSON data structure definitions for all services
```

## Anatomy of a Complete Task

```
gen_tasks/GXXX_task_name/
├── task.yaml                    # Task definition (copy from task_template.yaml, edit <<<CUSTOMIZE>>> sections)
├── grader.py                    # Grader (copy from grader_template.py, implement scoring logic)
└── fixtures/                    # Test data (one subdirectory per service)
    ├── gmail/inbox.json
    ├── calendar/events.json
    ├── contacts/contacts.json
    ├── todo/tasks.json
    ├── kb/articles.json
    ├── inventory/products.json
    ├── helpdesk/tickets.json
    ├── crm/customers.json
    ├── finance/transactions.json
    ├── scheduler/jobs.json
    ├── rss/articles.json
    ├── config/integrations.json
    └── notes/notes.json
```

## Steps to Create a New Task

### 1. Copy the templates

```bash
TASK_ID=G02_your_task_name
mkdir -p gen_tasks/${TASK_ID}/fixtures/{gmail,calendar,contacts,todo,kb,inventory,helpdesk,crm,finance,scheduler,rss,config,notes}
cp template/task_template.yaml gen_tasks/${TASK_ID}/task.yaml
cp template/grader_template.py gen_tasks/${TASK_ID}/grader.py
```

### 2. Write fixture data

Each service's fixture should contain:
- **Data directly related to the task** (small amount — the agent needs to find this information to complete the task)
- **A large amount of noise/distractor data** (simulates a real environment, tests the agent's information filtering ability)

See `fixture_schemas.yaml` for data structures.

### 3. Customize task.yaml

Edit all sections marked `<<<CUSTOMIZE>>>` in task.yaml:
- `task_id` / `task_name` / `category` / `difficulty`
- Fixture paths in `services[*].env` (replace `GXXX_task_name` with the actual directory name)
- `prompt.text` (task description)
- `scoring_components` (scoring components)
- `safety_checks` (safety constraints)
- `judge_rubric` / `reference_solution`
- `primary_dimensions`

### 4. Implement grader.py

Refer to `grader_template.py` and implement:
- `FORBIDDEN_TOOLS`: set of tools that must not be called during the task
- Completion scoring logic in the `grade()` method
- LLM judge rubrics (semantic evaluation criteria)
- Rule-based scoring (tool coverage, key information checks, etc.)

### 5. Run a test

```bash
cd claw-anything
source .venv/bin/activate
claw-anything run --task gen_tasks/${TASK_ID}/ --config config.yaml
```

## Design Principles

1. **All services, all tools**: All 13 services and 48 tools are exposed to the model — the agent must decide which ones to use
2. **Signal-to-noise ratio control**: Only a small amount of data in each fixture is directly relevant to the task; the rest is noise
3. **Cross-service information synthesis**: Completing a task typically requires gathering information from multiple services and reasoning across them
4. **Safety gate**: Clearly define which tools are forbidden — calling one sets safety=0
5. **Hybrid scoring**: Rule-based (tool coverage, key info) + LLM judge (semantic quality)
