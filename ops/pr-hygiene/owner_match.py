import re

_EXTERNAL_ID = re.compile(r"([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#([0-9]+)")
_PR_URL = re.compile(r"https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/pull/([0-9]+)")
_REPO_URL = re.compile(r"https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?")
_STALE_STATUSES = frozenset({"merged", "closed", "archived"})


def _key(repo, number):
    return "%s#%d" % (repo.lower(), int(number))


def _match_key(pattern, text):
    match = pattern.fullmatch(text) if isinstance(text, str) else None
    return _key(match.group(1), match.group(2)) if match else None


def _product_key(product):
    if product.get("type") != "pull_request" or product.get("provider") != "github":
        return None
    if product.get("status") in _STALE_STATUSES:
        return None
    external_id, url = product.get("externalId"), product.get("url")
    external_key = _match_key(_EXTERNAL_ID, external_id) if external_id is not None else None
    url_key = _match_key(_PR_URL, url) if url is not None else None
    if external_id is not None and external_key is None:
        return None
    if url is not None and url_key is None:
        return None
    if external_key and url_key and external_key != url_key:
        return None
    return external_key or url_key


def _registered_repos(card):
    urls = [card["workspace_repo"]] if card["workspace_repo"] else card["project_repos"]
    names = (_REPO_URL.fullmatch(url) if isinstance(url, str) else None for url in urls)
    return {match.group(1).lower() for match in names if match}


def _names_card(identifier, branch):
    pattern = r"(?<![0-9A-Za-z])%s(?![0-9])" % re.escape(identifier)
    return re.search(pattern, branch) is not None


def choose_owner(pr, candidates):
    unknown = [card["identifier"] for card in candidates if card["products"] is None]
    if unknown:
        return None, "facts unavailable, owner unknown: %s" % ", ".join(unknown)
    key = _key(pr["repo"], pr["number"])
    repo = pr["repo"].lower()
    same_repo_branch = (pr["head_repo"] or "").lower() == repo
    trusted, rejected = [], []
    for card in candidates:
        if key in {_product_key(product) for product in card["products"]}:
            trusted.append(card)
        elif _names_card(card["identifier"], pr["branch"]):
            if not same_repo_branch:
                rejected.append("%s: branch names it on a PR from another repo" % card["identifier"])
            elif repo in _registered_repos(card):
                trusted.append(card)
            else:
                rejected.append("%s: branch names it, not registered to this repo" % card["identifier"])
        else:
            rejected.append("%s: no repo-qualified provenance" % card["identifier"])
    if len(trusted) == 1:
        return trusted[0], None
    if trusted:
        return None, "ambiguous owner: %s" % ", ".join(card["identifier"] for card in trusted)
    return None, "no trusted owner: %s" % "; ".join(rejected)
