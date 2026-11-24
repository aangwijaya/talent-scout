"""Dispatch (GLOSSARY): the human's signal that one node may run now, checked before the service starts it.

The signal is the `run` label on the node's issue. A label is only text anyone with Issues write can add,
the agents included, so the service asks GitHub who added it (the issue timeline) and refuses any other
actor: the label comes off and the ledger records `dispatch.refused`.

A contract-change node needs one more thing: its `## Decision` section must start with "Approved", and the
issue body must have been last written by the human. The agents can edit issue bodies too, so the text
alone proves nothing; GitHub's edit history says who wrote it.
"""
import json
import re

from lib import REPOS, gh

RUN_LABEL = "run"
CONTRACT_LABEL = "contract-change"


class Refused(Exception):
    pass


def label_actor(node, label=RUN_LABEL):
    """Who added the label most recently, from the issue timeline (None if the timeline has no such event)."""
    pages = json.loads(gh("api", "--paginate", "--slurp",
                          f"repos/{REPOS[node.repo]}/issues/{node.number}/timeline?per_page=100") or "[]")
    events = [e for page in pages for e in page] if pages and isinstance(pages[0], list) else pages
    added = [e for e in events if e.get("event") == "labeled" and (e.get("label") or {}).get("name") == label]
    return ((added[-1].get("actor") or {}).get("login")) if added else None


def decision(body):
    """The first line of the issue's `## Decision` section, or ''."""
    match = re.search(r"^##\s*Decision\s*\n+(.*)$", body or "", re.M | re.I)
    return match.group(1).strip() if match else ""


def body_writer(node):
    """Who wrote the issue body as it stands: the last editor, or the author if it was never edited."""
    owner, name = REPOS[node.repo].split("/")
    query = ("query($o:String!,$n:String!,$i:Int!){repository(owner:$o,name:$n){issue(number:$i){"
             "author{login} editor{login}}}}")
    data = json.loads(gh("api", "graphql", "-f", f"query={query}", "-f", f"o={owner}", "-f", f"n={name}",
                         "-F", f"i={node.number}"))
    issue = data["data"]["repository"]["issue"]
    # `editor` is the last one to edit the body; it is null when nobody has edited it since it was opened.
    who = issue.get("editor") or issue.get("author") or {}
    return who.get("login")


def check(node, human):
    """Return who dispatched the node, or raise Refused with the reason."""
    actor = label_actor(node)
    if actor != human:
        raise Refused(f"the `{RUN_LABEL}` label was added by {actor or 'an unknown actor'}, not {human}")
    if node.has(CONTRACT_LABEL):
        line = decision(node.body)
        if not line.lower().startswith("approved"):
            raise Refused("a contract-change node needs `## Decision` to start with \"Approved\"")
        writer = body_writer(node)
        if writer != human:
            raise Refused(f"the issue body was last written by {writer}, so its Decision is not the human's")
    return actor
