#!/usr/bin/env python
import argparse
import asyncio
import os
import sys
from datetime import datetime

# Adjust path to import your modules
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from oracle3.data.live.live_data_source import LiveNewsDataSource


async def test_news_source(args):  # noqa: C901
    data_dir = args.data_dir
    os.makedirs(data_dir, exist_ok=True)

    # Check for API token
    api_token = args.api_token
    if not api_token:
        api_token = os.environ.get('NEWS_API_KEY')
        if not api_token:
            print(
                'Error: No API token provided. Use --api-token or set NEWS_API_KEY environment variable.'
            )
            return

    # Parse categories
    categories = args.categories.split(',') if args.categories else []

    # Create the data source
    data_source = LiveNewsDataSource(
        api_token=api_token,
        cache_file=os.path.join(data_dir, 'news_cache.jsonl'),
        polling_interval=args.interval,
        max_articles_per_poll=args.max_articles,
        categories=categories,
    )

    print(f'Starting LiveNewsDataSource (polling every {args.interval} seconds)')
    print(f'Categories: {", ".join(categories) if categories else "All"}')
    await data_source.start()

    # Event counter
    events_processed = 0

    print('Waiting for news events...')

    # Set up a timeout or event limit
    start_time = datetime.now()
    duration = args.duration
    max_events = args.max_events

    while True:
        # Check if we've reached our limits
        elapsed = (datetime.now() - start_time).total_seconds()
        if (duration > 0 and elapsed > duration) or (
            max_events > 0 and events_processed >= max_events
        ):
            break

        try:
            event = await data_source.get_next_event()

            if event:
                events_processed += 1

                # Display news event information
                print(f'\nNews Event #{events_processed}:')
                print(f'Title: {event.title}')
                print(f'Source: {event.source}')
                print(f'Published: {event.published_at}')

                if event.categories:
                    print(f'Categories: {", ".join(event.categories)}')

                if args.verbose:
                    print(f'Description: {event.description}')
                    print(f'URL: {event.url}')
                    if event.image_url:
                        print(f'Image: {event.image_url}')

                if events_processed % 5 == 0:
                    print(f'\nProcessed {events_processed} news events so far')

            else:
                # No event available, brief pause
                await asyncio.sleep(0.1)

        except KeyboardInterrupt:
            print('\nTest interrupted by user')
            break
        except Exception as e:
            print(f'Error: {e}')
            await asyncio.sleep(1.0)

    print('\nTest complete')
    print(f'Processed {events_processed} news events')
    print(f'Duration: {elapsed:.1f} seconds')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Test LiveNewsDataSource')
    parser.add_argument(
        '--api-token',
        type=str,
        default='',
        help='TheNewsAPI token (can also use NEWS_API_TOKEN env var)',
    )
    parser.add_argument(
        '--interval', type=float, default=60.0, help='Polling interval in seconds'
    )
    parser.add_argument(
        '--max-articles',
        type=int,
        default=10,
        help='Maximum articles to fetch per poll',
    )
    parser.add_argument(
        '--categories',
        type=str,
        default='',
        help='Comma-separated list of categories (e.g., business,politics)',
    )
    parser.add_argument(
        '--data-dir', type=str, default='data', help='Directory to store data'
    )
    parser.add_argument(
        '--duration',
        type=int,
        default=300,
        help='Test duration in seconds (0 for unlimited)',
    )
    parser.add_argument(
        '--max-events',
        type=int,
        default=20,
        help='Maximum number of events to process (0 for unlimited)',
    )
    parser.add_argument(
        '--verbose', action='store_true', help='Show detailed event information'
    )

    args = parser.parse_args()

    try:
        asyncio.run(test_news_source(args))
    except KeyboardInterrupt:
        print('\nScript interrupted')
