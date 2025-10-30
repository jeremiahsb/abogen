from abogen.integrations.calibre_opds import CalibreOPDSClient, feed_to_dict


def test_calibre_opds_feed_exposes_series_metadata() -> None:
    client = CalibreOPDSClient("http://example.com/catalog")
    xml_payload = """<?xml version=\"1.0\" encoding=\"UTF-8\"?>
    <feed xmlns=\"http://www.w3.org/2005/Atom\"
          xmlns:dc=\"http://purl.org/dc/terms/\"
          xmlns:calibre=\"http://calibre.kovidgoyal.net/2009/catalog\">
      <id>catalog</id>
      <title>Example Catalog</title>
      <entry>
        <id>book-1</id>
        <title>Sample Book</title>
        <calibre:series>The Expanse</calibre:series>
        <calibre:series_index>4</calibre:series_index>
        <link rel=\"http://opds-spec.org/acquisition\"
              href=\"books/sample.epub\"
              type=\"application/epub+zip\" />
      </entry>
    </feed>
    """

    feed = client._parse_feed(xml_payload, base_url="http://example.com/catalog")
    assert feed.entries, "Expected at least one entry in parsed feed"
    entry = feed.entries[0]

    assert entry.series == "The Expanse"
    assert entry.series_index == 4.0

    feed_dict = feed_to_dict(feed)
    assert feed_dict["entries"][0]["series"] == "The Expanse"
    assert feed_dict["entries"][0]["series_index"] == 4.0


def test_calibre_opds_feed_extracts_series_from_categories() -> None:
    client = CalibreOPDSClient("http://example.com/catalog")
    xml_payload = """<?xml version=\"1.0\" encoding=\"UTF-8\"?>
    <feed xmlns=\"http://www.w3.org/2005/Atom\"
          xmlns:dc=\"http://purl.org/dc/terms/\"
          xmlns:calibre=\"http://calibre.kovidgoyal.net/2009/catalog\">
      <id>catalog</id>
      <title>Example Catalog</title>
      <entry>
        <id>book-2</id>
        <title>Network Effect</title>
        <category
          scheme=\"http://calibre.kovidgoyal.net/2009/series\"
          term=\"The Murderbot Diaries #5\"
          label=\"The Murderbot Diaries [5]\" />
        <link rel=\"http://opds-spec.org/acquisition\"
              href=\"books/network-effect.epub\"
              type=\"application/epub+zip\" />
      </entry>
    </feed>
    """

    feed = client._parse_feed(xml_payload, base_url="http://example.com/catalog")
    assert feed.entries, "Expected at least one entry in parsed feed"
    entry = feed.entries[0]

    assert entry.series == "The Murderbot Diaries"
    assert entry.series_index == 5.0
