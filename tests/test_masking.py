from warehouse_mcp.masking import PiiPolicy

policy = PiiPolicy()


def test_column_name_matching():
    assert policy.is_pii_column("customer_email")
    assert policy.is_pii_column("Phone")
    assert not policy.is_pii_column("region")


def test_value_level_masking():
    text = "Email jo.smith@example.com, SSN 123-45-6789, card 4111-1111-1111-1111."
    assert policy.mask_text(text) == "Email j***@example.com, SSN ***-**-6789, card ****-****-****-1111."


def test_apply_masks_named_and_derived_columns():
    rows, masked = policy.apply(["id", "email", "contact"], [[1, "a@b.co", "x@y.io"], [2, None, None]], {2})
    assert rows == [[1, "***", "***"], [2, None, None]]
    assert masked == ["email", "contact"]
