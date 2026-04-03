/// Official Hejaz AI Assistant SDK for Flutter/Dart.
///
/// Provides streaming and non-streaming chat completions, session management,
/// and artifact access backed by the Hejaz AI Gateway.
///
/// ```dart
/// import 'package:hejaz_ai_sdk/hejaz_ai_sdk.dart';
///
/// final ai = HejazAI(apiKey: 'gw_xxx', tenantId: 'wahda');
///
/// // Non-streaming
/// final response = await ai.chat.send('What is Zakat?');
/// print(response.content);
///
/// // Streaming
/// await for (final event in ai.chat.stream('Explain fasting')) {
///   if (event.isText) {
///     stdout.write(event.textContent);
///   }
/// }
///
/// ai.dispose();
/// ```
library hejaz_ai_sdk;

export 'src/client.dart';
export 'src/chat.dart';
export 'src/models.dart';
export 'src/streaming.dart';
export 'src/exceptions.dart';
