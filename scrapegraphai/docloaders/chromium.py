import asyncio
from typing import Any, AsyncIterator, Iterator, List, Optional
from langchain_community.document_loaders.base import BaseLoader
from langchain_core.documents import Document
import aiohttp
import async_timeout
from typing import Union
from ..utils import Proxy, dynamic_import, get_logger, parse_or_search_proxy

logger = get_logger("web-loader")
logger.setLevel("INFO")

class ChromiumLoader(BaseLoader):
    """Scrapes HTML pages from URLs using a (headless) instance of the
    Chromium web driver with proxy protection.

    Attributes:
        backend: The web driver backend library; defaults to 'playwright'.
        browser_config: A dictionary containing additional browser kwargs.
        headless: Whether to run browser in headless mode.
        proxy: A dictionary containing proxy settings; None disables protection.
        urls: A list of URLs to scrape content from.
        requires_js_support: Flag to determine if JS rendering is required.
    """

    RETRY_LIMIT = 3
    TIMEOUT = 10

    def __init__(
        self,
        urls: List[str],
        *,
        backend: str = "playwright",
        headless: bool = True,
        proxy: Optional[Proxy] = None,
        load_state: str = "domcontentloaded",
        requires_js_support: bool = False,
        **kwargs: Any,
    ):
        """Initialize the loader with a list of URL paths.

        Args:
            backend: The web driver backend library; defaults to 'playwright'.
            headless: Whether to run browser in headless mode.
            proxy: A dictionary containing proxy information; None disables protection.
            urls: A list of URLs to scrape content from.
            requires_js_support: Whether to use JS rendering for scraping.
            kwargs: A dictionary containing additional browser kwargs.

        Raises:
            ImportError: If the required backend package is not installed.
        """
        message = (
            f"{backend} is required for ChromiumLoader. "
            f"Please install it with `pip install {backend}`."
        )

        dynamic_import(backend, message)

        self.backend = backend
        self.browser_config = kwargs
        self.headless = headless
        self.proxy = parse_or_search_proxy(proxy) if proxy else None
        self.urls = urls
        self.load_state = load_state
        self.requires_js_support = requires_js_support

    async def ascrape_undetected_chromedriver(self, url: str) -> str:
        """
        Asynchronously scrape the content of a given URL using undetected chrome with Selenium.

        Args:
            url (str): The URL to scrape.

        Returns:
            str: The scraped HTML content or an error message if an exception occurs.
        """
        import undetected_chromedriver as uc

        logger.info(f"Starting scraping with {self.backend}...")
        results = ""
        attempt = 0

        while attempt < self.RETRY_LIMIT:
            try:
                async with async_timeout.timeout(self.TIMEOUT):
                    driver = uc.Chrome(headless=self.headless)
                    driver.get(url)
                    results = driver.page_source
                    logger.info(f"Successfully scraped {url}")
                    break
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                attempt += 1
                logger.error(f"Attempt {attempt} failed: {e}")
                if attempt == self.RETRY_LIMIT:
                    results = f"Error: Network error after {self.RETRY_LIMIT} attempts - {e}"
            finally:
                driver.quit()

        return results

    async def ascrape_playwright_scroll(
        self, 
        url: str, 
        timeout: Union[int, None]=30, 
        scroll: int=15000,
        sleep: float=2,
        scroll_to_bottom: bool=False
    ) -> str:
        """
        Asynchronously scrape the content of a given URL using Playwright's sync API and scrolling.

        Notes: 
        - The user gets to decide between scrolling to the bottom of the page or scrolling by a finite amount of time.
        - If the user chooses to scroll to the bottom, the scraper will stop when the page height stops changing or when
        the timeout is reached. In this case, the user should opt for an appropriate timeout value i.e. larger than usual.
        - Sleep needs to be set to a value greater than 0 to allow lazy-loaded content to load.
        Additionally, if used with scroll_to_bottom=True, the sleep value should be set to a higher value, to
        make sure that the scrolling actually happens, thereby allowing the page height to change.
        - Probably the best website to test this is https://www.reddit.com/ as it has infinite scrolling.

        Args:
        - url (str): The URL to scrape.
        - timeout (Union[int, None]): The maximum time to spend scrolling. This is separate from the global timeout. If set, must be greater than 0.
        Can also be set to None, in which case the scraper will only stop when the page height stops changing.
        - scroll (float): The number of pixels to scroll down by. Defaults to 15000. Cannot be less than 5000 pixels.
        Less than this and we don't scroll enough to see any content change.
        - sleep (int): The number of seconds to sleep after each scroll, to allow the page to load.
        Defaults to 2. Must be greater than 0.

        Returns:
            str: The scraped HTML content

        Raises:
        - ValueError: If the timeout value is less than or equal to 0.
        - ValueError: If the sleep value is less than or equal to 0.
        - ValueError: If the scroll value is less than 5000.
        """
        # NB: I have tested using scrollHeight to determine when to stop scrolling
        # but it doesn't always work as expected. The page height doesn't change on some sites like 
        # https://www.steelwood.amsterdam/. The site deos not scroll to the bottom.
        # In my browser I can scroll vertically but in Chromium it scrolls horizontally?!?

        if timeout and timeout <= 0:
            raise ValueError("If set, timeout value for scrolling scraper must be greater than 0.")
        
        if sleep <= 0:
            raise ValueError("Sleep for scrolling scraper value must be greater than 0.")
        
        if scroll < 5000:
            raise ValueError("Scroll value for scrolling scraper must be greater than or equal to 5000.")
        
        from playwright.async_api import async_playwright
        from undetected_playwright import Malenia
        import time

        logger.info(f"Starting scraping with scrolling support for {url}...")

        results = ""
        attempt = 0

        while attempt < self.RETRY_LIMIT:
            try:
                async with async_playwright() as p:
                    browser = await p.chromium.launch(
                        headless=self.headless, proxy=self.proxy, **self.browser_config
                    )
                    context = await browser.new_context()
                    await Malenia.apply_stealth(context)
                    page = await context.new_page()
                    await page.goto(url, wait_until="domcontentloaded")
                    await page.wait_for_load_state(self.load_state)

                    previous_height = None
                    start_time = time.time()

                    # Store the heights of the page after each scroll
                    # This is useful in case we scroll with a timer and want to stop shortly after reaching the bottom
                    # or simly when the page stops changing for some reason.
                    heights = []

                    while True:
                        current_height = await page.evaluate("document.body.scrollHeight")
                        heights.append(current_height)
                        heights = heights[-5:] # Keep only the last 5 heights, to not run out of memory

                        # Break if we've reached the bottom of the page i.e. if scrolling makes no more progress
                        # Attention!!! This is not always reliable. Sometimes the page might not change due to lazy loading
                        # or other reasons. In such cases, the user should set scroll_to_bottom=False and set a timeout.
                        if scroll_to_bottom and previous_height == current_height:
                            logger.info(f"Reached bottom of page for url {url}")
                            break

                        previous_height = current_height

                        await page.mouse.wheel(0, scroll)
                        logger.debug(f"Scrolled {url} to current height {current_height}px...")
                        time.sleep(sleep)  # Allow some time for any lazy-loaded content to load

                        current_time = time.time()
                        elapsed_time = current_time - start_time
                        logger.debug(f"Elapsed time: {elapsed_time} seconds")

                        if timeout:
                            if elapsed_time >= timeout:
                                logger.info(f"Reached timeout of {timeout} seconds for url {url}")
                                break
                            elif len(heights) == 5 and len(set(heights)) == 1:
                                logger.info(f"Page height has not changed for url {url} for the last 5 scrolls. Stopping.")
                                break
                    
                    results = await page.content()
                    break

            except (aiohttp.ClientError, asyncio.TimeoutError, Exception) as e:
                attempt += 1
                logger.error(f"Attempt {attempt} failed: {e}")
                if attempt == self.RETRY_LIMIT:
                    results = f"Error: Network error after {self.RETRY_LIMIT} attempts - {e}"
            finally:
                await browser.close()

        return results

    async def ascrape_playwright(self, url: str) -> str:
        """
        Asynchronously scrape the content of a given URL using Playwright's async API.

        Args:
            url (str): The URL to scrape.

        Returns:
            str: The scraped HTML content or an error message if an exception occurs.
        """
        from playwright.async_api import async_playwright
        from undetected_playwright import Malenia

        logger.info(f"Starting scraping with {self.backend}...")
        results = ""
        attempt = 0

        while attempt < self.RETRY_LIMIT:
            try:
                async with async_playwright() as p, async_timeout.timeout(self.TIMEOUT):
                    browser = await p.chromium.launch(
                        headless=self.headless, proxy=self.proxy, **self.browser_config
                    )
                    context = await browser.new_context()
                    await Malenia.apply_stealth(context)
                    page = await context.new_page()
                    await page.goto(url, wait_until="domcontentloaded")
                    await page.wait_for_load_state(self.load_state)
                    results = await page.content()
                    logger.info("Content scraped")
                    break
            except (aiohttp.ClientError, asyncio.TimeoutError, Exception) as e:
                attempt += 1
                logger.error(f"Attempt {attempt} failed: {e}")
                if attempt == self.RETRY_LIMIT:
                    results = f"Error: Network error after {self.RETRY_LIMIT} attempts - {e}"
            finally:
                await browser.close()

        return results

    async def ascrape_with_js_support(self, url: str) -> str:
        """
        Asynchronously scrape the content of a given URL by rendering JavaScript using Playwright.

        Args:
            url (str): The URL to scrape.

        Returns:
            str: The fully rendered HTML content after JavaScript execution, 
            or an error message if an exception occurs.
        """
        from playwright.async_api import async_playwright

        logger.info(f"Starting scraping with JavaScript support for {url}...")
        results = ""
        attempt = 0

        while attempt < self.RETRY_LIMIT:
            try:
                async with async_playwright() as p, async_timeout.timeout(self.TIMEOUT):
                    browser = await p.chromium.launch(
                        headless=self.headless, proxy=self.proxy, **self.browser_config
                    )
                    context = await browser.new_context()
                    page = await context.new_page()
                    await page.goto(url, wait_until="networkidle")
                    results = await page.content()
                    logger.info("Content scraped after JavaScript rendering")
                    break
            except (aiohttp.ClientError, asyncio.TimeoutError, Exception) as e:
                attempt += 1
                logger.error(f"Attempt {attempt} failed: {e}")
                if attempt == self.RETRY_LIMIT:
                    results = f"Error: Network error after {self.RETRY_LIMIT} attempts - {e}"
            finally:
                await browser.close()

        return results

    def lazy_load(self) -> Iterator[Document]:
        """
        Lazily load text content from the provided URLs.

        This method yields Documents one at a time as they're scraped,
        instead of waiting to scrape all URLs before returning.

        Yields:
            Document: The scraped content encapsulated within a Document object.
        """
        scraping_fn = (
            self.ascrape_with_js_support if self.requires_js_support else getattr(self, f"ascrape_{self.backend}")
        )

        for url in self.urls:
            html_content = asyncio.run(scraping_fn(url))
            metadata = {"source": url}
            yield Document(page_content=html_content, metadata=metadata)

    async def alazy_load(self) -> AsyncIterator[Document]:
        """
        Asynchronously load text content from the provided URLs.

        This method leverages asyncio to initiate the scraping of all provided URLs
        simultaneously. It improves performance by utilizing concurrent asynchronous
        requests. Each Document is yielded as soon as its content is available,
        encapsulating the scraped content.

        Yields:
            Document: A Document object containing the scraped content, along with its
            source URL as metadata.
        """
        scraping_fn = (
            self.ascrape_with_js_support if self.requires_js_support else getattr(self, f"ascrape_{self.backend}")
        )

        tasks = [scraping_fn(url) for url in self.urls]
        results = await asyncio.gather(*tasks)
        for url, content in zip(self.urls, results):
            metadata = {"source": url}
            yield Document(page_content=content, metadata=metadata)
