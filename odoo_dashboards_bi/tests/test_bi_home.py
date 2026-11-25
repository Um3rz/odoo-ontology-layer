from odoo.tests import HttpCase, tagged


@tagged("post_install", "-at_install")
class TestBiHome(HttpCase):
    def test_backend_boots_with_the_bi_home_page(self):
        # browser_js fails on any JS error, including a module that could not be started.
        self.browser_js(
            "/web",
            "if (!odoo.__DEBUG__.services['@web/core/registry'].registry.category('od_homescreen.pages').contains('bi-ai'))"
            " { throw new Error('bi-ai page missing'); } console.log('test successful');",
            ready="odoo.isReady",
            login="admin",
        )
