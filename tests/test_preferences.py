import pytest
from fnv_relay import preferences
from fnv_relay.store import Store


@pytest.mark.parametrize('choice,expected',[('1','largest'),('2','smallest'),('3','random')])
def test_task_order_menu_saves_selected_default(tmp_path, choice, expected):
    store=Store(tmp_path)
    assert preferences.choose_order(store,ask=lambda _:choice,show=lambda _:None)==expected
    assert store.config()['default_task_order']==expected
    store.close()


def test_saved_order_does_not_prompt_again_and_explicit_order_overrides(tmp_path,monkeypatch):
    store=Store(tmp_path); store.set_config({'paths':{},'default_task_order':'smallest'})
    monkeypatch.setattr(preferences,'choose_order',lambda *_:pytest.fail('saved order should not prompt'))
    assert preferences.start_order(store)==('smallest',False)
    assert preferences.start_order(store,'random')==('random',True)
    assert store.config()['default_task_order']=='random'
    store.close()


def test_invalid_answer_reprompts_without_changing_saved_choice(tmp_path):
    store=Store(tmp_path); answers=iter(['oops','2'])
    assert preferences.choose_order(store,ask=lambda _:next(answers),show=lambda _:None)=='smallest'
    store.close()
