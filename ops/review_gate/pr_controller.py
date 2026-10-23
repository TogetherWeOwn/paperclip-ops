#!/usr/bin/env python3
"""Inventory-only slice. No controller state machine, card actions or runtime imports included."""
import json
import subprocess
QUERY = 'query($o:String!,$r:String!,$cursor:String){repository(owner:$o,name:$r){isArchived pullRequests(states:OPEN,first:20,after:$cursor,\norderBy:{field:CREATED_AT,direction:ASC}){totalCount pageInfo{hasNextPage endCursor} nodes{number title url isDraft headRefOid mergeable mergeStateStatus createdAt labels(first:20){nodes{name}}\nbaseRefName headRefName headRef{target{... on Commit{oid committedDate statusCheckRollup{contexts(first:100){pageInfo{hasNextPage} nodes{\n... on CheckRun{name status conclusion detailsUrl databaseId\ncheckSuite{workflowRun{databaseId workflow{databaseId}}}}\n... on StatusContext{context state targetUrl}}}}}}}}}}}'

def gh(args, env):
    return subprocess.run(['gh'] + args, capture_output=True, text=True, env=env).stdout

def fetch_repository(full, env, query=None):
    owner, name = full.split('/', 1)
    cursor, nodes, cursors = (None, [], set())
    for _ in range(100):
        args = ['api', 'graphql', '-f', f'query={query or QUERY}', '-F', f'o={owner}', '-F', f'r={name}']
        if cursor:
            args += ['-f', f'cursor={cursor}']
        payload = json.loads(gh(args, env))
        if payload.get('errors'):
            raise ValueError('GraphQL returned errors; partial inventory rejected')
        repo = payload['data']['repository']
        if not repo:
            return None
        connection = repo['pullRequests']
        nodes.extend(connection['nodes'])
        page = connection['pageInfo']
        if not page['hasNextPage']:
            connection['nodes'] = list({p['number']: p for p in nodes}.values())
            connection['totalCount'] = len(connection['nodes'])
            return repo
        cursor = page.get('endCursor')
        if not cursor or cursor in cursors:
            raise ValueError('Invalid or repeated PR pagination cursor')
        cursors.add(cursor)
    raise ValueError('PR inventory exceeded pagination bound')
