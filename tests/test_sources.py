"""Source-specific discovery regressions found during live staging."""
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from scraper import browse_page


class RoyalRoadDiscoveryTests(unittest.TestCase):
    def test_chapter_table_supplies_titles_and_excludes_reviews_and_recommendations(self):
        url = 'https://www.royalroad.com/fiction/47038/book-of-the-dead'
        html = '''<html><title>Book Of The Dead</title>
        <a href="/fiction/47038/book-of-the-dead/chapter/100/prelude">Start Reading</a>
        <table id="chapters"><tr><td><a href="/fiction/47038/book-of-the-dead/chapter/100/prelude">Prelude</a></td>
        <td><a href="/fiction/47038/book-of-the-dead/chapter/100/prelude">6 months ago</a></td></tr>
        <tr><td><a href="/fiction/47038/book-of-the-dead/chapter/101/first">Chapter One</a></td></tr></table>
        <a href="/fiction/999/other/chapter/200/wrong">Chapter from another book</a>
        <ul class="pagination"><li><a href="?reviews=2">Review page 2</a></li>
        <li><a href="?comments=2">Comments</a></li><li><a href="?page=2">Chapter page 2</a></li></ul></html>'''
        with patch('scraper._fetch', return_value=SimpleNamespace(text=html, url=url, headers={'Content-Type': 'text/html'})):
            result = browse_page(url)
        self.assertEqual([x['title'] for x in result['links']], ['Prelude', 'Chapter One'])
        self.assertEqual(result['pages'], [url + '?page=2'])

    def test_missing_table_still_scopes_chapter_links_to_requested_fiction(self):
        url = 'https://www.royalroad.com/fiction/42/novel'
        html = '<a href="/fiction/42/novel/chapter/1/one">Chapter 1</a><a href="/fiction/43/other/chapter/2/two">Chapter 2</a>'
        with patch('scraper._fetch', return_value=SimpleNamespace(text=html, url=url, headers={})):
            result = browse_page(url)
        self.assertEqual(len(result['links']), 1)
        self.assertIn('/fiction/42/', result['links'][0]['url'])

    def test_real_cover_precedes_generic_social_image_in_document_order(self):
        html='<head><meta property="og:image" content="/logo.svg"></head><body><div class="fiction-cover"><img src="https://cdn.example/book.webp"></div></body>'
        url='https://www.royalroad.com/fiction/42'
        with patch('scraper._fetch',return_value=SimpleNamespace(text=html,url=url,headers={})):
            self.assertEqual(browse_page(url)['cover_url'],'https://cdn.example/book.webp')

    def test_social_cover_remains_fallback_when_source_image_is_missing(self):
        url='https://public.example/book'
        with patch('scraper._fetch',return_value=SimpleNamespace(text='<meta property="og:image" content="/cover.png">',url=url,headers={})):
            self.assertEqual(browse_page(url)['cover_url'],'https://public.example/cover.png')


class FreeWebNovelDiscoveryTests(unittest.TestCase):
    def test_full_chapter_list_wins_over_read_first_and_unrelated_books(self):
        url='https://freewebnovel.com/novel/example'
        html='<h1 class="tit">Real title</h1><a href="/novel/example/chapter-1">Read first</a><ul class="ul-list5"><li><a href="/novel/example/chapter-3">Chapter 3</a></li><li><a href="/novel/example/chapter-1">Chapter 1</a></li><li><a href="/novel/other/chapter-2">Other book</a></li><li><a href="/novel/example/chapter-2">Chapter 2</a></li></ul>'
        with patch('scraper._fetch',return_value=SimpleNamespace(text=html,url=url,headers={})):
            result=browse_page(url)
        self.assertEqual(result['title'],'Real title')
        self.assertEqual([r['title'] for r in result['links']],['Chapter 1','Chapter 2','Chapter 3'])

    def test_chapter_url_returns_to_book_and_preserves_http_error(self):
        url='https://freewebnovel.com/novel/example/chapter-12'
        with patch('scraper._fetch',side_effect=ValueError('Source returned HTTP 403')) as fetch:
            with self.assertRaisesRegex(ValueError,'HTTP 403'):browse_page(url)
        fetch.assert_called_once_with('https://freewebnovel.com/novel/example')


class ErrorChapterTests(unittest.TestCase):
    def test_soft404_is_rejected_but_story_named_error404_is_valid(self):
        from scraper import scrape_chapter
        url='https://freewebnovel.com/novel/example/chapter-1'
        error='<html><h1>404 Not Found</h1><div class="txt">'+('This page is unavailable. '*20)+'</div></html>'
        with patch('scraper._fetch',return_value=SimpleNamespace(text=error,url=url,headers={})):
            with self.assertRaisesRegex(ValueError,'HTTP200'):scrape_chapter(url)
        story='<html><h1>Novel title</h1><div class="txt"><h4>Chapter 1: Error 404</h4><p>'+('Subject404 lived in a laboratory and dreamed of freedom. '*20)+'</p></div></html>'
        with patch('scraper._fetch',return_value=SimpleNamespace(text=story,url=url,headers={})):
            chapter=scrape_chapter(url)
        self.assertEqual(chapter['chapter_title'],'Chapter 1: Error 404');self.assertIn('Subject404',chapter['content'])

class FreeWebNovelPaginationTests(unittest.TestCase):
    url='https://freewebnovel.com/novel/example'
    def response(self,text,url=None,kind='text/html'):
        return SimpleNamespace(text=text,url=url or self.url,headers={'Content-Type':kind})

    def test_current_public_list_excludes_latest_and_uses_ajax_next(self):
        html='''<div id="indexListPage" data-current-page="1" data-total-page="2" data-page-size="40"></div>
        <ul class="ul-list5"><a href="/novel/example/chapter-80">Latest</a></ul>
        <ul id="idData"><a href="/novel/example/chapter-1">Chapter 1</a></ul>
        <script type="application/ld+json">{"@type":"Book","url":"https://freewebnovel.com/novel/other","name":"Wrong","author":{"name":"Wrong"}}</script>
        <script type="application/ld+json">{"@type":"Book","url":"https://freewebnovel.com/novel/example","name":"Correct","author":[{"name":"Author"}],"image":"/real.jpg"}</script>'''
        with patch('scraper._fetch',return_value=self.response(html)):r=browse_page(self.url)
        self.assertEqual([a['title'] for a in r['links']],['Chapter 1'])
        self.assertEqual(r['pages'],[self.url+'?ajax=chapters&page=2&pageSize=40'])
        self.assertEqual((r['title'],r['author'],r['cover_url']),('Correct','Author','https://freewebnovel.com/real.jpg'))

    def test_public_ajax_fragment_has_validated_page_and_order(self):
        import json
        url=self.url+'?ajax=chapters&page=2&pageSize=40'
        data={'code':200,'page':2,'pageSize':40,'totalPage':2,'html':'<a href="/novel/example/chapter-42">Chapter 42</a><a href="/novel/example/chapter-41">Chapter 41</a>'}
        with patch('scraper._fetch',return_value=self.response(json.dumps(data),url,'application/json')):r=browse_page(url)
        self.assertEqual([a['title'] for a in r['links']],['Chapter 41','Chapter 42']);self.assertEqual(r['pages'],[])
        data['page']=1
        with patch('scraper._fetch',return_value=self.response(json.dumps(data),url,'application/json')):
            with self.assertRaisesRegex(ValueError,'pagination failed'):browse_page(url)

    def test_empty_free_catalog_is_an_error_not_zero_success(self):
        with patch('scraper._fetch',return_value=self.response('<h1>Unavailable</h1>')):
            with self.assertRaisesRegex(ValueError,'no usable chapter list'):browse_page(self.url)
