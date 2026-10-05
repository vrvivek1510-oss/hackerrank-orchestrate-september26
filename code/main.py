import csv
import math
import os
from collections import defaultdict
from datetime import date, datetime, timedelta

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
DATASET_DIR = os.path.join(ROOT, 'dataset')


def parse_float(value):
    if value is None:
        return 0.0
    value = str(value).strip()
    if not value:
        return 0.0
    try:
        return float(value)
    except ValueError:
        return 0.0


def parse_date(value):
    if value is None or str(value).strip() == '':
        return None
    try:
        return date.fromisoformat(str(value).strip())
    except ValueError:
        return None


def money_str(value):
    return format(value, '.2f')


def read_csv(path):
    with open(path, newline='', encoding='utf-8') as f:
        return list(csv.DictReader(f))


def load_profiles():
    profiles = {}
    for row in read_csv(os.path.join(DATASET_DIR, 'financial_profiles.csv')):
        user_id = row['user_id']
        profiles[user_id] = {
            'user_id': user_id,
            'home_currency': row['home_currency'],
            'current_available_balance': parse_float(row['current_available_balance']),
            'minimum_balance_to_keep': parse_float(row['minimum_balance_to_keep']),
            'financial_priorities': (row['financial_priorities'] or ''),
            'expense_categories_to_protect': set((row['expense_categories_to_protect'] or '').split('|')) if row['expense_categories_to_protect'] else set(),
            'expense_categories_user_is_willing_to_reduce': set((row['expense_categories_user_is_willing_to_reduce'] or '').split('|')) if row['expense_categories_user_is_willing_to_reduce'] else set(),
            'expense_categories_user_is_willing_to_stop': set((row['expense_categories_user_is_willing_to_stop'] or '').split('|')) if row['expense_categories_user_is_willing_to_stop'] else set(),
            'payment_methods_user_will_consider': [p.strip() for p in (row['payment_methods_user_will_consider'] or '').split('|') if p.strip()],
            'max_installment_months': row['max_installment_months'],
        }
    return profiles


def load_event_map():
    events_by_user = defaultdict(list)
    for row in read_csv(os.path.join(DATASET_DIR, 'financial_events.csv')):
        row = dict(row)
        row['amount'] = parse_float(row.get('amount'))
        row['event_date'] = parse_date(row.get('event_date'))
        row['settlement_date'] = parse_date(row.get('settlement_date'))
        events_by_user[row['user_id']].append(row)
    return events_by_user


def load_request_options():
    options_by_request = defaultdict(list)
    for row in read_csv(os.path.join(DATASET_DIR, 'request_payment_options.csv')):
        row = dict(row)
        row['payment_amount'] = parse_float(row.get('payment_amount'))
        row['number_of_payments'] = int(row['number_of_payments']) if row.get('number_of_payments') else 0
        row['financing_fee'] = parse_float(row.get('financing_fee'))
        row['total_payable_amount'] = parse_float(row.get('total_payable_amount'))
        row['first_payment_date'] = parse_date(row.get('first_payment_date'))
        row['payment_frequency_days'] = int(row['payment_frequency_days']) if row.get('payment_frequency_days') else 0
        options_by_request[row['request_id']].append(row)
    return options_by_request


PROFILES = load_profiles()
EVENTS_BY_USER = load_event_map()
OPTIONS_BY_REQUEST = load_request_options()
REQUESTS = read_csv(os.path.join(DATASET_DIR, 'requests.csv'))
SAMPLE_REQUESTS = read_csv(os.path.join(DATASET_DIR, 'sample_requests.csv'))


def user_events(user_id):
    return EVENTS_BY_USER.get(user_id, [])


def event_date_for_cash(event):
    return event.get('settlement_date') or event.get('event_date')


def normalize_event_delta(event):
    amount = parse_float(event.get('amount'))
    if event.get('direction') == 'debit':
        return -amount
    if event.get('direction') == 'credit':
        return amount
    return 0.0


def cash_timeline_by_user(user_id, request_date, horizon_days=90, forced_changes=None):
    forced_changes = forced_changes or {}
    timeline = defaultdict(float)
    for event in user_events(user_id):
        if event.get('direction') not in ('credit', 'debit'):
            continue
        if event.get('status') in ('failed', 'cancelled', 'unrealized'):
            continue
        if event.get('status') == 'pending' and event.get('direction') == 'credit':
            continue
        d = event_date_for_cash(event)
        if d is None or d < request_date:
            continue
        if d > request_date + timedelta(days=horizon_days):
            continue
        event_id = event.get('event_id')
        if event_id in forced_changes:
            action = forced_changes[event_id]
            if action == 'stop':
                continue
            if action.startswith('reduce_to:'):
                try:
                    new_amount = parse_float(action.split(':', 1)[1])
                except Exception:
                    new_amount = 0.0
                if event.get('direction') == 'debit':
                    timeline[d] += -new_amount
                else:
                    timeline[d] += new_amount
                continue
        timeline[d] += normalize_event_delta(event)
    return timeline


def simulate_cash(user_id, request_date, payment_day, payment_amount, horizon_days=90, forced_changes=None):
    profile = PROFILES[user_id]
    forced_changes = forced_changes or {}
    balance = profile['current_available_balance']
    # Honor pending debits as reserved cash before any payment decision.
    for event in user_events(user_id):
        if event.get('direction') != 'debit' or event.get('status') != 'pending':
            continue
        d = event_date_for_cash(event)
        if d is not None and request_date <= d <= request_date + timedelta(days=horizon_days):
            balance -= parse_float(event.get('amount'))
    timeline = cash_timeline_by_user(user_id, request_date, horizon_days, forced_changes)
    # Apply the payment on the target day, then all same-day cash events.
    dates = sorted(set([payment_day] + list(timeline.keys())))
    for d in dates:
        if d < request_date:
            continue
        if d == payment_day:
            balance -= payment_amount
            if balance < profile['minimum_balance_to_keep']:
                return False
        balance += timeline.get(d, 0.0)
        if balance < profile['minimum_balance_to_keep']:
            return False
    return True


def amount_safe_today(user_id, request_date, requested_amount, horizon_days=90):
    profile = PROFILES[user_id]
    lo, hi = 0.0, min(requested_amount, max(profile['current_available_balance'], 0.0))
    for _ in range(70):
        mid = (lo + hi) / 2.0
        if simulate_cash(user_id, request_date, request_date, mid, horizon_days):
            lo = mid
        else:
            hi = mid
    return lo


def earliest_safe_full_payment_date(user_id, request_date, requested_amount, latest_day=None):
    latest_day = latest_day or (request_date + timedelta(days=90))
    d = request_date
    while d <= latest_day:
        if simulate_cash(user_id, request_date, d, requested_amount, 90):
            return d
        d += timedelta(days=1)
    return None


def payment_option_date_series(option, request_date):
    if not option.get('number_of_payments'):
        return []
    start = option.get('first_payment_date') or request_date
    step = option.get('payment_frequency_days') or 0
    return [start + timedelta(days=i * step) for i in range(option['number_of_payments'])]


def installment_plan_is_safe(user_id, request_date, option):
    profile = PROFILES[user_id]
    balance = profile['current_available_balance']
    cash = defaultdict(float)
    for event in user_events(user_id):
        if event.get('direction') not in ('credit', 'debit'):
            continue
        if event.get('status') in ('failed', 'cancelled', 'unrealized'):
            continue
        if event.get('status') == 'pending' and event.get('direction') == 'credit':
            continue
        d = event_date_for_cash(event)
        if d is None or d < request_date or d > request_date + timedelta(days=90):
            continue
        cash[d] += normalize_event_delta(event)
    for event in user_events(user_id):
        if event.get('direction') == 'debit' and event.get('status') == 'pending':
            d = event_date_for_cash(event)
            if d is not None and request_date <= d <= request_date + timedelta(days=90):
                balance -= parse_float(event.get('amount'))
    for d in payment_option_date_series(option, request_date):
        cash[d] += -option['payment_amount']
    for d in sorted(cash):
        if d < request_date:
            continue
        balance += cash[d]
        if balance < profile['minimum_balance_to_keep']:
            return False
    return True


def choose_best_installment(user_id, request_id, request_date):
    profile = PROFILES[user_id]
    options = OPTIONS_BY_REQUEST.get(request_id, [])
    eligible = []
    max_months = profile['max_installment_months']
    for option in options:
        if option['payment_method'] != 'installments':
            continue
        if 'installments' not in profile['payment_methods_user_will_consider']:
            continue
        if max_months:
            try:
                max_limit = int(max_months)
            except ValueError:
                max_limit = 999
            months = max(1, int(math.ceil(option['number_of_payments'] * max(option['payment_frequency_days'], 1) / 30.0)))
            if months > max_limit:
                continue
        if installment_plan_is_safe(user_id, request_date, option):
            eligible.append(option)
    if not eligible:
        return None
    return min(eligible, key=lambda o: (
        o['total_payable_amount'],
        min(payment_option_date_series(o, request_date)),
        o['number_of_payments'],
        o['payment_option_id']
    ))


def suggest_spending_changes(user_id, request_date, requested_amount, payment_day):
    profile = PROFILES[user_id]
    candidates = []
    for event in user_events(user_id):
        if event.get('direction') != 'debit':
            continue
        if event.get('status') in ('failed', 'cancelled', 'unrealized'):
            continue
        cat = event.get('category') or ''
        if cat in profile['expense_categories_to_protect']:
            continue
        if event.get('status') == 'pending' and event.get('direction') == 'credit':
            continue
        d = event_date_for_cash(event)
        if d is None or d < request_date:
            continue
        if cat in profile['expense_categories_user_is_willing_to_stop'] or cat in profile['expense_categories_user_is_willing_to_reduce']:
            candidates.append(event)
    for event in sorted(candidates, key=lambda e: parse_float(e.get('amount')) or 0.0, reverse=True):
        cat = event.get('category') or ''
        if cat in profile['expense_categories_user_is_willing_to_stop']:
            forced = {event['event_id']: 'stop'}
            if simulate_cash(user_id, request_date, payment_day, requested_amount, 90, forced):
                return [f"stop:{event['event_id']}"]
        if cat in profile['expense_categories_user_is_willing_to_reduce']:
            current_amount = parse_float(event.get('amount'))
            minimum_allowed = parse_float(event.get('minimum_allowed_amount')) if event.get('minimum_allowed_amount') else 0.0
            new_amount = max(minimum_allowed, current_amount * 0.5)
            forced = {event['event_id']: f'reduce_to:{new_amount:.2f}'}
            if simulate_cash(user_id, request_date, payment_day, requested_amount, 90, forced):
                return [f"reduce_to:{event['event_id']}:{new_amount:.2f}"]
    return []


def format_plan(plan):
    return '|'.join(f"{d.isoformat()}:{money_str(amount)}" for d, amount in plan)


def choose_recommendation_for_request(row):
    user_id = row['user_id']
    profile = PROFILES[user_id]
    request_date = parse_date(row['request_date'])
    desired_completion = parse_date(row['desired_completion_date'])
    requested_amount = parse_float(row['requested_amount'])
    allowed_methods = set(profile['payment_methods_user_will_consider'])
    safe_today = amount_safe_today(user_id, request_date, requested_amount)
    safe_today = min(safe_today, requested_amount)

    if safe_today >= requested_amount and 'full_payment' in allowed_methods:
        return {
            'request_id': row['request_id'],
            'amount_safe_to_pay': money_str(requested_amount),
            'affordability_status': 'affordable_now',
            'recommended_payment_method': 'full_payment',
            'payment_plan': f"{request_date.isoformat()}:{money_str(requested_amount)}",
            'earliest_date_for_full_payment': request_date.isoformat(),
            'spending_changes_needed': 'none',
            'decision_explanation': 'The full amount is safe on the request date without cutting flexible spending.'
        }

    earliest_full = earliest_safe_full_payment_date(user_id, request_date, requested_amount, desired_completion)
    if earliest_full is None:
        earliest_full = earliest_safe_full_payment_date(user_id, request_date, requested_amount, request_date + timedelta(days=90))

    # Partial payment option, if allowed and beneficial.
    if str(row.get('allows_partial_payment', '')).lower() == 'true' and 'partial_payment' in allowed_methods and 0 < safe_today < requested_amount:
        if earliest_full is not None and earliest_full <= desired_completion:
            remaining = requested_amount - safe_today
            return {
                'request_id': row['request_id'],
                'amount_safe_to_pay': money_str(safe_today),
                'affordability_status': 'affordable_with_plan',
                'recommended_payment_method': 'partial_payment',
                'payment_plan': f"{request_date.isoformat()}:{money_str(safe_today)}|{earliest_full.isoformat()}:{money_str(remaining)}",
                'earliest_date_for_full_payment': earliest_full.isoformat(),
                'spending_changes_needed': 'none',
                'decision_explanation': f"A partial payment is safe now and the remaining balance can be settled on {earliest_full.isoformat()} without breaching the minimum-balance rule."
            }

    best_installment = choose_best_installment(user_id, row['request_id'], request_date)
    if best_installment is not None and 'installments' in allowed_methods:
        plan = [(d, best_installment['payment_amount']) for d in payment_option_date_series(best_installment, request_date)]
        return {
            'request_id': row['request_id'],
            'amount_safe_to_pay': money_str(safe_today),
            'affordability_status': 'affordable_with_plan',
            'recommended_payment_method': 'installments',
            'payment_plan': format_plan(plan),
            'earliest_date_for_full_payment': earliest_full.isoformat() if earliest_full else '',
            'spending_changes_needed': 'none',
            'decision_explanation': f"The available installment option {best_installment['payment_option_id']} keeps the balance above the minimum while spreading the cost over time."
        }

    if earliest_full is not None and 'full_payment' in allowed_methods and earliest_full <= desired_completion:
        return {
            'request_id': row['request_id'],
            'amount_safe_to_pay': money_str(safe_today),
            'affordability_status': 'affordable_later',
            'recommended_payment_method': 'wait',
            'payment_plan': f"{earliest_full.isoformat()}:{money_str(requested_amount)}",
            'earliest_date_for_full_payment': earliest_full.isoformat(),
            'spending_changes_needed': 'none',
            'decision_explanation': f"The full amount becomes safe on {earliest_full.isoformat()} based on the forecasted cash flow, so waiting is the safest eligible recommendation."
        }

    changes = suggest_spending_changes(user_id, request_date, requested_amount, request_date)
    if changes and 'full_payment' in allowed_methods:
        return {
            'request_id': row['request_id'],
            'amount_safe_to_pay': money_str(safe_today),
            'affordability_status': 'affordable_with_plan',
            'recommended_payment_method': 'full_payment',
            'payment_plan': f"{request_date.isoformat()}:{money_str(requested_amount)}",
            'earliest_date_for_full_payment': request_date.isoformat(),
            'spending_changes_needed': '|'.join(changes),
            'decision_explanation': 'Flexible recurring spending can be adjusted sufficiently to make the full payment safe while keeping the minimum required balance intact.'
        }

    return {
        'request_id': row['request_id'],
        'amount_safe_to_pay': money_str(safe_today),
        'affordability_status': 'not_affordable',
        'recommended_payment_method': 'not_recommended',
        'payment_plan': 'none',
        'earliest_date_for_full_payment': '' if earliest_full is None else earliest_full.isoformat(),
        'spending_changes_needed': 'none',
        'decision_explanation': 'No eligible safe payment path is available within the forecast period without exceeding the minimum balance threshold.'
    }


def main():
    rows = []
    for request in REQUESTS:
        rows.append(choose_recommendation_for_request(request))

    out_path = os.path.join(ROOT, 'output.csv')
    fieldnames = [
        'request_id', 'amount_safe_to_pay', 'affordability_status', 'recommended_payment_method',
        'payment_plan', 'earliest_date_for_full_payment', 'spending_changes_needed', 'decision_explanation'
    ]
    with open(out_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({
                'request_id': row['request_id'],
                'amount_safe_to_pay': row['amount_safe_to_pay'],
                'affordability_status': row['affordability_status'],
                'recommended_payment_method': row['recommended_payment_method'],
                'payment_plan': row['payment_plan'],
                'earliest_date_for_full_payment': row['earliest_date_for_full_payment'],
                'spending_changes_needed': row['spending_changes_needed'],
                'decision_explanation': row['decision_explanation'],
            })


if __name__ == '__main__':
    main()
