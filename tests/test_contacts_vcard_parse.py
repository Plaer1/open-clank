from routes.contacts_routes import _parse_vcards


def test_apple_item_grouped_properties_parsed():
    vcf = (
        "BEGIN:VCARD\nVERSION:3.0\nFN:Jane Doe\n"
        "item1.EMAIL;type=INTERNET;type=pref:jane@example.com\n"
        "item2.TEL;type=CELL;type=pref:+15550100\n"
        "UID:abc-123\nEND:VCARD\n"
    )
    c = _parse_vcards(vcf)[0]
    assert c["emails"] == ["jane@example.com"]
    assert c["phones"] == ["+15550100"]
    assert c["uid"] == "abc-123"


def test_plain_ungrouped_properties_still_parsed():
    vcf = (
        "BEGIN:VCARD\nVERSION:3.0\nFN:John Smith\n"
        "EMAIL;TYPE=INTERNET:john@example.com\n"
        "TEL;TYPE=CELL:+15550199\n"
        "UID:xyz\nEND:VCARD\n"
    )
    c = _parse_vcards(vcf)[0]
    assert c["name"] == "John Smith"
    assert c["emails"] == ["john@example.com"]
    assert c["phones"] == ["+15550199"]
    assert c["uid"] == "xyz"


def test_folded_email_is_reassembled():
    vcard = (
        "BEGIN:VCARD\r\n"
        "VERSION:3.0\r\n"
        "FN:John Doe\r\n"
        "EMAIL;TYPE=INTERNET:john.doe.with.a.very.long.local.part@exampledomain\r\n"
        " .com\r\n"
        "END:VCARD\r\n"
    )
    contacts = _parse_vcards(vcard)
    assert len(contacts) == 1
    assert contacts[0]["emails"] == [
        "john.doe.with.a.very.long.local.part@exampledomain.com"
    ]


def test_folded_display_name_is_reassembled():
    vcard = (
        "BEGIN:VCARD\n"
        "FN:A Very Long Display Name That The Server\n"
        "  Decided To Fold\n"
        "EMAIL:x@y.com\n"
        "END:VCARD\n"
    )
    c = _parse_vcards(vcard)[0]
    assert c["name"] == "A Very Long Display Name That The Server Decided To Fold"


def test_unfolded_vcard_still_parses():
    vcard = "BEGIN:VCARD\nFN:Jane\nEMAIL:jane@z.com\nTEL:+15550001\nEND:VCARD\n"
    c = _parse_vcards(vcard)[0]
    assert c["name"] == "Jane"
    assert c["emails"] == ["jane@z.com"]
    assert c["phones"] == ["+15550001"]
