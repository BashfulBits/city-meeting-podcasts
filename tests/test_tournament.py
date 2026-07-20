from citypods.tournament import R5_FLASH_MODEL, contest_plan, persisted_r5_flash_output


def test_round_robin_has_three_independent_judges():
    assert len(contest_plan()) == 3
    assert all(judge not in {left, right} for left, right, judge in contest_plan())


def test_reuses_only_provenanced_r5_flash_candidates():
    candidate = {"provider_model": R5_FLASH_MODEL, "recipe_hash": "r5-recipe", "id": "housing"}
    assert persisted_r5_flash_output(
        {"tags_llm_recipe_hash": "r5-recipe", "llm_tag_candidates": [candidate]}
    ) == [candidate]
    assert (
        persisted_r5_flash_output({"tags_llm_recipe_hash": "r5-recipe", "llm_tag_candidates": []})
        is None
    )
    assert (
        persisted_r5_flash_output(
            {
                "tags_llm_recipe_hash": "r5-recipe",
                "llm_tag_candidates": [{**candidate, "provider_model": "litellm:old-model"}],
            }
        )
        is None
    )
