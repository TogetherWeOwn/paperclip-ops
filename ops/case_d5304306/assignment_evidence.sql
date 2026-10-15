-- ops/case_d5304306/assignment_evidence.sql — case_d5304306 (assignment host source, slice 1)
--
-- Read-only source for the assignment detector. One statement; it returns one
-- JSON document per row and build_assignment_bundle.py turns those rows into
-- watchdog.assignment-evidence-input.v1. It writes nothing, selects no secret,
-- no config and no statement text. activity_log.details is read only through
-- the status keys below, never as a whole.
--
-- psql variable: company_id (run_assignment_evidence.sh sets it).
-- Row kinds: focusDocument (0 or 1), project (all), issue (all, hidden ones
-- included so that ancestor walks can pass through them).
--
-- Translation subset (offline suite only; production runs Postgres): :'company_id',
-- json_build_object, #>> with a literal path, CAST(... AS text), WITH, COALESCE,
-- ROW_NUMBER() OVER, LEFT JOIN, UNION ALL.

WITH focus_doc AS (
  SELECT dr.id AS revision_id, dr.body
  FROM issue_documents AS idoc
  JOIN documents AS d ON d.id = idoc.document_id
  JOIN document_revisions AS dr ON dr.id = d.latest_revision_id
  WHERE idoc.company_id = :'company_id'
    AND idoc.issue_id = '00000000-0000-4000-8000-000000000126'
    AND idoc.key = 'focus-membership'
),
status_moves AS (
  SELECT m.id, m.entity_id, m.created_at, m.to_status
  FROM (
    SELECT a.id, a.entity_id, a.created_at,
           COALESCE(a.details #>> '{status}', a.details #>> '{changes,status,to}') AS to_status,
           COALESCE(a.details #>> '{_previous,status}', a.details #>> '{changes,status,from}') AS from_status
    FROM activity_log AS a
    WHERE a.company_id = :'company_id'
      AND a.entity_type = 'issue'
      AND a.action = 'issue.updated'
  ) AS m
  WHERE m.to_status IS NOT NULL
    AND m.from_status IS NOT NULL
    AND m.to_status <> m.from_status
),
latest_move AS (
  SELECT ranked.id, ranked.entity_id, ranked.created_at, ranked.to_status
  FROM (
    SELECT s.id, s.entity_id, s.created_at, s.to_status,
           ROW_NUMBER() OVER (PARTITION BY s.entity_id ORDER BY s.created_at DESC, s.id DESC) AS rn
    FROM status_moves AS s
  ) AS ranked
  WHERE ranked.rn = 1
),
ready_pr AS (
  SELECT ranked.issue_id, ranked.id
  FROM (
    SELECT w.issue_id, w.id,
           ROW_NUMBER() OVER (PARTITION BY w.issue_id ORDER BY w.updated_at DESC, w.id DESC) AS rn
    FROM issue_work_products AS w
    WHERE w.company_id = :'company_id'
      AND w.type = 'pull_request'
      AND w.status = 'ready_for_review'
  ) AS ranked
  WHERE ranked.rn = 1
)
SELECT json_build_object(
  'kind', 'focusDocument',
  'revisionId', CAST(f.revision_id AS text),
  'body', f.body
) AS doc
FROM focus_doc AS f
UNION ALL
SELECT json_build_object(
  'kind', 'project',
  'id', CAST(p.id AS text)
) AS doc
FROM projects AS p
WHERE p.company_id = :'company_id'
UNION ALL
SELECT json_build_object(
  'kind', 'issue',
  'id', CAST(i.id AS text),
  'identifier', i.identifier,
  'parentId', CAST(i.parent_id AS text),
  'projectId', CAST(i.project_id AS text),
  'status', i.status,
  'statusVersion', i.status_version,
  'createdAt', i.created_at,
  'hiddenAt', i.hidden_at,
  'assigneeAgentId', CAST(i.assignee_agent_id AS text),
  'assigneeUserId', i.assignee_user_id,
  'readyPullRequestId', CAST(rp.id AS text),
  'lastMoveId', CAST(lm.id AS text),
  'lastMoveAt', lm.created_at,
  'lastMoveTo', lm.to_status
) AS doc
FROM issues AS i
LEFT JOIN ready_pr AS rp ON rp.issue_id = i.id
LEFT JOIN latest_move AS lm ON lm.entity_id = CAST(i.id AS text)
WHERE i.company_id = :'company_id';
