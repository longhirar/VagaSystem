# VagaSystem — Central de Controle

Sistema de monitoramento de vagas de estacionamento via MQTT, com interface de linha de comando.

---

## Visão Geral

O **VagaSystem** é uma central de controle que se comunica em tempo real com sensores instalados em vagas de estacionamento através do protocolo **MQTT**.

## DOC INTERNA

A main inicia chamando o start_mqtt, que inicia a thread de rede em background.

### start_mqtt

Quando o cliente conecta no broker, definimos a variavel connected como true e fazemos o subscribe no tópico. Usamos a função auxiliar \_add_log para adicionar mensagens ao log_messages (uma lista de strings global).

Usando o state_lock, ordenamos os valores das vagas (que são um dicionário de dicionários) por id_vaga e exibimos na tela as últimas 5 mensagens do log, assim como o menu.

Quando o cliente se desconecta, apenas setamos a flag de connected pra false e informamos ao user.

Quando há uma mensagem recebida, atualizamos o dicionário de vagas utilizando o state_lock novamente, dessa vez por conta da escrita. Além disso, utilizamos um try catch aqui para evitar problemas com caracteres não reconhecidos no payload.

Dentro do dicionário de vagas, armazenamos um um dicionário com o id da vaga, se está ocupada, distância, luz ambiente, brilho led e último update formatado para hora, minuto e segundo. Em seguida adicionamos ao log com o \_add_log.

No reconnect_delay_set, estabelecemos um delay mínimo de 2 segundos e máximo de 30 segundos para reconexão.

Por fim, nos conectamos ao broker e iniciamos a thread de rede em background com loop_start().

## Loop principal

Voltando ao loop principal, declaramos uma variável screen que recebe o valor de "dashboard" ou "detail" dependendo do estado atual, para nos permitir desenhar a tela correta.

Iniciamos então um loop infinito que desenha utilizando a função draw_dashboard ou draw_detail dependendo do valor da variável screen. Em seguida, aguardamos a entrada do usuário utilizando a função input().

Para permitir com que o usuário publique comandos de uma vaga em específico, permitimos a visualização de detalhes de uma vaga através da inserção do seu dígito, e ao fazer isso, a tela de detalhes é aberta, fazendo com que os comandos publicados lá sejam específicos a somente uma vaga.

Os comandos são publicados através da função publish_command, que recebe o client, o tópico e o comando. Ela é somente um wrapper para facilitar e reduzir a reescrita de código, já que debaixo dos panos apenas chamamos o publish do client com um json.dumps do comando passado. Dentro da função aproveitamos e adicionamos um log do comando para um determinado tópico.
