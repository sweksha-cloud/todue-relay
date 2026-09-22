// Turns the view model into Google's card widgets. Deliberately thin: no decisions live here, so the
// part that can only be checked by looking at Gmail stays as small as possible.

import { dateFieldName } from "./format";
import type { ErrorModel, HomeModel } from "./format";

type Card = GoogleAppsScript.Card_Service.Card;

function refreshFooter() {
  const refresh = CardService.newAction().setFunctionName("onRefresh");
  return CardService.newFixedFooter().setPrimaryButton(
    CardService.newTextButton().setText("Refresh").setOnClickAction(refresh),
  );
}

export function renderHome(model: HomeModel): Card {
  const card = CardService.newCardBuilder().setHeader(
    CardService.newCardHeader().setTitle(model.title).setSubtitle(model.subtitle),
  );
  for (const s of model.sections) {
    const section = CardService.newCardSection().setHeader(s.title);
    if (s.items.length === 0) section.addWidget(CardService.newTextParagraph().setText(s.emptyText));
    for (const item of s.items) {
      const widget = CardService.newDecoratedText().setText(item.title).setWrapText(true);
      if (item.tag) widget.setTopLabel(item.tag);
      if (item.subtitle) widget.setBottomLabel(item.subtitle);
      section.addWidget(widget);
      if (item.needsDatetime) {
        // One of this item's buttons (Reschedule / Schedule) needs a date/time typed in first.
        // Its own field name (dateFieldName) keeps it from colliding with any other item's field
        // on the same card; onAction reads it back by the emailId on the button that was pressed.
        section.addWidget(
          CardService.newTextInput()
            .setFieldName(dateFieldName(item.emailId))
            .setTitle("Date & time (e.g. 2026-10-05 14:30)"),
        );
      }
      if (item.buttons.length > 0) {
        const set = CardService.newButtonSet();
        for (const b of item.buttons) {
          const action = CardService.newAction().setFunctionName("onAction").setParameters({ emailId: b.emailId, action: b.action });
          set.addButton(CardService.newTextButton().setText(b.label).setOnClickAction(action));
        }
        section.addWidget(set);
      }
    }
    if (s.more) section.addWidget(CardService.newTextParagraph().setText(s.more));
    card.addSection(section);
  }
  return card.setFixedFooter(refreshFooter()).build();
}

export function renderError(model: ErrorModel): Card {
  const section = CardService.newCardSection()
    .addWidget(CardService.newTextParagraph().setText(model.message))
    .addWidget(CardService.newTextParagraph().setText(model.hint));
  return CardService.newCardBuilder()
    .setHeader(CardService.newCardHeader().setTitle("ToDue Relay").setSubtitle("Can't load"))
    .addSection(section)
    .setFixedFooter(refreshFooter())
    .build();
}
