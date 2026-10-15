import unittest

from owner_match import choose_owner

WEB = "TogetherWeOwn/web-app"
PLUGIN = "TogetherWeOwn/plugin-capacity"
WEB_WORKSPACE = "https://github.com/TogetherWeOwn/web-app.git"
PLUGIN_WORKSPACE = "https://github.com/TogetherWeOwn/plugin-capacity.git"


def pr(repo, number, branch="feat/role-exclusions", head_repo=None):
    return {"repo": repo, "number": number, "branch": branch,
            "head_repo": repo if head_repo is None else head_repo}


def external(ref, status="active"):
    return {"type": "pull_request", "provider": "github", "status": status,
            "externalId": ref, "url": None}


def canonical(url, status="active"):
    return {"type": "pull_request", "provider": "github", "status": status,
            "externalId": None, "url": url}


def card(identifier, products=(), workspace=None, project=(), facts=True):
    return {
        "identifier": identifier,
        "iid": "id-" + identifier,
        "status": "todo",
        "agent": "agent",
        "has_edge": "f",
        "products": list(products) if facts else None,
        "workspace_repo": workspace,
        "project_repos": list(project),
    }


class OwnerMatchTest(unittest.TestCase):
    def assertOwner(self, identifier, owner_pr, candidates):
        owner, reason = choose_owner(owner_pr, candidates)
        self.assertIsNotNone(owner, reason)
        self.assertEqual(owner["identifier"], identifier)

    def assertNoOwner(self, owner_pr, candidates):
        owner, reason = choose_owner(owner_pr, candidates)
        self.assertIsNone(owner)
        self.assertTrue(reason)

    def test_incidental_mention_of_a_card_never_wakes_it(self):
        self.assertNoOwner(pr(PLUGIN, 13), [card("case_a26a33be", workspace=WEB_WORKSPACE)])

    def test_equal_pr_numbers_in_two_repos_map_to_their_own_cards(self):
        web = card("case_a26a33be", [external(WEB + "#13")], WEB_WORKSPACE)
        plugin = card("case_c6f9007b", [canonical("https://github.com/" + PLUGIN + "/pull/13")],
                      PLUGIN_WORKSPACE)
        self.assertOwner("case_c6f9007b", pr(PLUGIN, 13), [web, plugin])
        self.assertOwner("case_a26a33be", pr(WEB, 13, branch="feat/other"), [web, plugin])

    def test_two_cards_with_the_same_repo_qualified_pr_are_ambiguous(self):
        first = card("case_f3e5a3c5", [external(WEB + "#21")], WEB_WORKSPACE)
        second = card("case_121c4087", [external(WEB + "#21")], WEB_WORKSPACE)
        self.assertNoOwner(pr(WEB, 21), [first, second])

    def test_stale_and_conflicting_provenance_is_not_trusted(self):
        stale = card("case_804ba044", [external(WEB + "#22", status="merged")], WEB_WORKSPACE)
        conflicting = card("case_5d6ccbbe", [{
            "type": "pull_request", "provider": "github", "status": "active",
            "externalId": WEB + "#22",
            "url": "https://github.com/TogetherWeOwn/web-app/pull/23",
        }], WEB_WORKSPACE)
        self.assertNoOwner(pr(WEB, 22, branch="feat/other"), [stale, conflicting])

    def test_repo_qualified_provenance_maps_by_external_id_or_url(self):
        by_external = card("case_3a788350", [external(WEB + "#30")], WEB_WORKSPACE)
        by_url = card("case_37610113", [canonical("https://github.com/" + WEB + "/pull/31")],
                      WEB_WORKSPACE)
        self.assertOwner("case_3a788350", pr(WEB, 30, branch="feat/a"), [by_external, by_url])
        self.assertOwner("case_37610113", pr(WEB, 31, branch="feat/b"), [by_external, by_url])

    def test_branch_naming_card_registered_to_the_same_repo_owns_the_pr(self):
        named = card("case_bb401cbb", project=[WEB_WORKSPACE])
        self.assertOwner("case_bb401cbb", pr(WEB, 32, branch="case_bb401cbb-fix"), [named])

    def test_branch_naming_card_registered_elsewhere_is_rejected(self):
        named = card("case_9d0bd5f3", workspace=WEB_WORKSPACE)
        self.assertNoOwner(pr(PLUGIN, 40, branch="feat/case_9d0bd5f3-role"), [named])

    def test_workspace_registration_is_authoritative_over_project(self):
        named = card("case_9ba121ce", workspace=WEB_WORKSPACE, project=[PLUGIN_WORKSPACE])
        self.assertNoOwner(pr(PLUGIN, 41, branch="case_9ba121ce-x"), [named])

    def test_branch_naming_two_registered_cards_is_ambiguous(self):
        first = card("case_092cd51d", workspace=WEB_WORKSPACE)
        second = card("case_b7328ab7", workspace=WEB_WORKSPACE)
        self.assertNoOwner(pr(WEB, 42, branch="feat/case_092cd51d-and-case_b7328ab7"), [first, second])

    def test_branch_naming_card_without_any_registration_is_rejected(self):
        self.assertNoOwner(pr(WEB, 43, branch="case_149a7b02-x"), [card("case_149a7b02")])

    def test_branch_naming_card_on_a_fork_pr_never_owns_the_pr(self):
        named = card("case_8de2a25e", workspace=WEB_WORKSPACE)
        self.assertNoOwner(pr(WEB, 44, branch="case_8de2a25e-x", head_repo="outsider/web-app"), [named])
        self.assertNoOwner(dict(pr(WEB, 45, branch="case_8de2a25e-x"), head_repo=None), [named])

    def test_branch_match_is_case_sensitive_like_the_candidate_search(self):
        named = card("case_59f71c03", workspace=WEB_WORKSPACE)
        self.assertNoOwner(pr(WEB, 48, branch="case_59f71c03-x"), [named])

    def test_non_string_provenance_is_ignored_not_fatal(self):
        odd_product = card("case_998c1411", [{"type": "pull_request", "provider": "github", "status": "active",
                                         "externalId": 4242, "url": None}], WEB_WORKSPACE)
        odd_repo = card("case_63da8c4b", workspace=12345)
        self.assertNoOwner(pr(WEB, 46, branch="feat/x"), [odd_product])
        self.assertNoOwner(pr(WEB, 47, branch="case_63da8c4b-x"), [odd_repo])

    def test_unavailable_card_facts_fail_closed(self):
        unavailable = card("case_a1517994", workspace=WEB_WORKSPACE, facts=False)
        self.assertNoOwner(pr(WEB, 50, branch="case_a1517994-x"), [unavailable])

    def test_unavailable_candidate_blocks_a_branch_owner_it_cannot_rule_out(self):
        unknown = card("case_aa7bcebe", facts=False)
        named = card("case_bb401cbb", workspace=WEB_WORKSPACE)
        self.assertNoOwner(pr(WEB, 52, branch="case_bb401cbb-fix"), [unknown, named])

    def test_identifier_match_respects_the_number_boundary(self):
        self.assertNoOwner(pr(WEB, 51, branch="case_9ba121ce-x"),
                           [card("case_ccba9d4c", workspace=WEB_WORKSPACE)])

    def test_valid_url_with_malformed_external_id_is_ignored(self):
        mixed = card("case_1f74fc71", [{
            "type": "pull_request", "provider": "github", "status": "active",
            "externalId": "web-app 33",
            "url": "https://github.com/" + WEB + "/pull/33",
        }], WEB_WORKSPACE)
        self.assertNoOwner(pr(WEB, 33, branch="feat/other"), [mixed])

    def test_valid_external_id_with_non_pr_url_is_ignored(self):
        mixed = card("case_6c6124ae", [{
            "type": "pull_request", "provider": "github", "status": "active",
            "externalId": WEB + "#34",
            "url": "https://example.com/not-a-pr",
        }], WEB_WORKSPACE)
        self.assertNoOwner(pr(WEB, 34, branch="feat/other"), [mixed])

    def test_non_pr_or_non_github_product_is_ignored(self):
        branch_product = card("case_6359f6bc", [{
            "type": "branch", "provider": "github", "status": "active",
            "externalId": WEB + "#35", "url": None,
        }], WEB_WORKSPACE)
        foreign_product = card("case_ebb8d534", [{
            "type": "pull_request", "provider": "gitlab", "status": "active",
            "externalId": WEB + "#36", "url": None,
        }], WEB_WORKSPACE)
        self.assertNoOwner(pr(WEB, 35, branch="feat/other"), [branch_product])
        self.assertNoOwner(pr(WEB, 36, branch="feat/other"), [foreign_product])

    def test_branch_prefix_does_not_name_the_card(self):
        named = card("case_fcea9973", workspace=WEB_WORKSPACE)
        self.assertNoOwner(pr(WEB, 53, branch="XTOG-1034-x"), [named])


if __name__ == "__main__":
    unittest.main()
